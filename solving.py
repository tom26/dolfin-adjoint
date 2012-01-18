import ufl
import ufl.classes
import ufl.algorithms
import ufl.operators

import dolfin.fem.solving
import dolfin

import libadjoint
import libadjoint.exceptions

import hashlib
import time

import assembly

debugging={}

class CoeffStore(object):
  '''This object manages the mapping from Dolfin coefficients to libadjoint Variables.
  In the process, it also manages the incrementing of the timestep associated with each
  variable, so that the user does not have to manually manage the time information.'''
  def __init__(self):
    self.coeffs={}

  def next(self, coeff):
    '''Increment the timestep corresponding to the provided Dolfin
    coefficient and then return the corresponding libadjoint variable.'''

    try:
      self.coeffs[coeff]+=1
    except KeyError:
      self.coeffs[coeff]=0

    return libadjoint.Variable(str(coeff), self.coeffs[coeff], 0)

  def __getitem__(self, coeff):
    '''Return the libadjoint variable corresponding to coeff.'''

    if not self.coeffs.has_key(coeff):
      self.coeffs[coeff]=0

    return libadjoint.Variable(str(coeff), self.coeffs[coeff], 0)

adj_variables=CoeffStore()

# Set record_all to true to enable recording all variables in the forward
# run. This is primarily useful for debugging.
debugging["record_all"] = False
debugging["test_hermitian"] = None
debugging["test_derivative"] = None
debugging["fussy_replay"] = True

# Create the adjointer, the central object that records the forward solve
# as it happens.
adjointer = libadjoint.Adjointer()

# A dictionary that saves the functionspaces of all checkpoint variables that have been saved to disk
checkpoint_fs = {}

def annotate(*args, **kwargs):
  '''This routine handles all of the annotation, recording the solves as they
  happen so that libadjoint can rewind them later.'''

  if isinstance(args[0], ufl.classes.Equation):
    # annotate !

    # Unpack the arguments, using the same routine as the real Dolfin solve call
    unpacked_args = dolfin.fem.solving._extract_args(*args, **kwargs)
    eq = unpacked_args[0]
    u  = unpacked_args[1]
    bcs = unpacked_args[2]
    J = unpacked_args[3]

    if isinstance(eq.lhs, ufl.Form) and isinstance(eq.rhs, ufl.Form):
      eq_lhs = eq.lhs
      eq_rhs = eq.rhs
      eq_bcs = bcs
      linear = True
    else:
      eq_lhs, eq_rhs = define_nonlinear_equation(eq.lhs, u)
      F = eq.lhs
      eq_bcs = []
      linear = False

    newargs = list(args)

    try:
      solver_params = kwargs["solver_parameters"]
      ksp = solver_params["linear_solver"]
    except KeyError:
      ksp = None

    try:
      solver_params = kwargs["solver_parameters"]
      pc = solver_params["preconditioner"]
    except KeyError:
      pc = None

  elif isinstance(args[0], dolfin.cpp.Matrix):
    linear = True
    try:
      eq_lhs = assembly.assembly_cache[args[0]]
    except KeyError:
      raise libadjoint.exceptions.LibadjointErrorInvalidInputs("dolfin_adjoint did not assemble your form, and so does not recognise your matrix. Did you from dolfin_adjoint import *?")

    try:
      eq_rhs = assembly.assembly_cache[args[2]]
    except KeyError:
      raise libadjoint.exceptions.LibadjointErrorInvalidInputs("dolfin_adjoint did not assemble your form, and so does not recognise your right-hand side. Did you from dolfin_adjoint import *?")

    u = args[1]
    if not isinstance(u, dolfin.Function):
      raise libadjoint.exceptions.LibadjointErrorInvalidInputs("dolfin_adjoint needs the Function you are solving for, rather than its .vector().")
    newargs = list(args)
    newargs[1] = u.vector() # for the real solve later

    try:
      ksp = args[3]
    except IndexError:
      ksp = None

    try:
      pc = args[4]
    except IndexError:
      pc = None

    eq_bcs = list(set(assembly.bc_cache[args[0]] + assembly.bc_cache[args[2]]))
  else:
    raise libadjoint.exceptions.LibadjointErrorNotImplemented("Don't know how to annotate your equation, sorry!")

  # Suppose we are solving for a variable w, and that variable shows up in the
  # coefficients of eq_lhs/eq_rhs.
  # Does that mean:
  #  a) the /previous value/ of that variable, and you want to timestep?
  #  b) the /value to be solved for/ in this solve?
  # i.e. is it timelevel n-1, or n?
  # if Dolfin is doing a linear solve, we want case a);
  # if Dolfin is doing a nonlinear solve, we want case b).
  # so if we are doing a nonlinear solve, we bump the timestep number here
  # /before/ we map the coefficients -> dependencies,
  # so that libadjoint records the dependencies with the right timestep number.
  if not linear:
    var = adj_variables.next(u)

  # Set up the data associated with the matrix on the left-hand side. This goes on the diagonal
  # of the 'large' system that incorporates all of the timelevels, which is why it is prefixed
  # with diag.
  diag_name = hashlib.md5(str(eq_lhs) + str(eq_rhs) + str(u) + str(time.time())).hexdigest() # we don't have a useful human-readable name, so take the md5sum of the string representation of the forms
  diag_deps = [adj_variables[coeff] for coeff in ufl.algorithms.extract_coefficients(eq_lhs) if hasattr(coeff, "function_space")]
  diag_coeffs = [coeff for coeff in ufl.algorithms.extract_coefficients(eq_lhs) if hasattr(coeff, "function_space")]
  diag_block = libadjoint.Block(diag_name, dependencies=diag_deps, test_hermitian=debugging["test_hermitian"], test_derivative=debugging["test_derivative"])

  # Similarly, create the object associated with the right-hand side data.
  if linear:
    rhs = RHS(eq_rhs)
  else:
    rhs = NonlinearRHS(eq_rhs, F, u, bcs, mass=eq_lhs)

  # We need to check if this is the first equation,
  # so that we can register the appropriate initial conditions.
  # These equations are necessary so that libadjoint can assemble the
  # relevant adjoint equations for the adjoint variables associated with
  # the initial conditions.
  for coeff, dep in zip(rhs.coefficients(),rhs.dependencies()) + zip(diag_coeffs, diag_deps):
    # If coeff is not known, it must be an initial condition.
    if not adjointer.variable_known(dep):

      if not linear: # don't register initial conditions for the first nonlinear solve.
        if dep == var:
          continue

      fn_space = coeff.function_space()
      block_name = "Identity: %s" % str(fn_space)
      identity_block = libadjoint.Block(block_name)
    
      init_rhs=Vector(coeff).duplicate()
      init_rhs.axpy(1.0,Vector(coeff))

      def identity_assembly_cb(variables, dependencies, hermitian, coefficient, context):
        assert coefficient == 1
        return (Matrix(ufl.Identity(fn_space.dim())), Vector(dolfin.Function(fn_space)))

      identity_block.assemble=identity_assembly_cb

      if debugging["record_all"]:
        adjointer.record_variable(dep, libadjoint.MemoryStorage(Vector(coeff)))

      initial_eq = libadjoint.Equation(dep, blocks=[identity_block], targets=[dep], rhs=RHS(init_rhs))
      adjointer.register_equation(initial_eq)
      assert adjointer.variable_known(dep)


  # c.f. the discussion above. In the linear case, we want to bump the
  # timestep number /after/ all of the dependencies' timesteps have been
  # computed for libadjoint.
  if linear:
    var = adj_variables.next(u)

  # With the initial conditions out of the way, let us now define the callbacks that
  # define the actions of the operator the user has passed in on the lhs of this equation.

  def diag_assembly_cb(dependencies, values, hermitian, coefficient, context):
    '''This callback must conform to the libadjoint Python block assembly
    interface. It returns either the form or its transpose, depending on
    the value of the logical hermitian.'''

    assert coefficient == 1

    value_coeffs=[v.data for v in values]
    eq_l=dolfin.replace(eq_lhs, dict(zip(diag_coeffs, value_coeffs)))

    if hermitian:
      # Homogenise the adjoint boundary conditions. This creates the adjoint
      # solution associated with the lifted discrete system that is actually solved.
      adjoint_bcs = [dolfin.homogenize(bc) for bc in eq_bcs if isinstance(bc, dolfin.DirichletBC)]
      if len(adjoint_bcs) == 0: adjoint_bcs = None
      return (Matrix(dolfin.adjoint(eq_l), bcs=adjoint_bcs, pc=pc, ksp=ksp), Vector(None, fn_space=u.function_space()))
    else:
      return (Matrix(eq_l, bcs=eq_bcs, pc=pc, ksp=ksp), Vector(None, fn_space=u.function_space()))
  diag_block.assemble = diag_assembly_cb

  def diag_action_cb(dependencies, values, hermitian, coefficient, input, context):
    value_coeffs = [v.data for v in values]
    eq_l = dolfin.replace(eq_lhs, dict(zip(diag_coeffs, value_coeffs)))

    if hermitian:
      eq_l = dolfin.adjoint(eq_l)

    output_vec = dolfin.assemble(coefficient * dolfin.action(eq_l, input.data))
    output_fn = dolfin.Function(input.data.function_space())
    vec = output_fn.vector()
    for i in range(len(vec)):
      vec[i] = output_vec[i]

    return Vector(output_fn)
  diag_block.action = diag_action_cb

  if len(diag_deps) > 0:
    # If this block is nonlinear (the entries of the matrix on the LHS
    # depend on any variable previously computed), then that will induce
    # derivative terms in the adjoint equations. Here, we define the
    # callback libadjoint will need to compute such terms.
    def derivative_action(dependencies, values, variable, contraction_vector, hermitian, input, coefficient, context):
      dolfin_variable = values[dependencies.index(variable)].data
      dolfin_values = [val.data for val in values]

      current_form = dolfin.replace(eq_lhs, dict(zip(diag_coeffs, dolfin_values)))

      deriv = dolfin.derivative(current_form, dolfin_variable)
      args = ufl.algorithms.extract_arguments(deriv)
      deriv = dolfin.replace(deriv, {args[1]: contraction_vector.data}) # contract over the middle index

      if hermitian:
        deriv = dolfin.adjoint(deriv)

      action = coefficient * dolfin.action(deriv, input.data)

      return Vector(action)
    diag_block.derivative_action = derivative_action

  eqn = libadjoint.Equation(var, blocks=[diag_block], targets=[var], rhs=rhs)

  cs = adjointer.register_equation(eqn)
  if cs == int(libadjoint.constants.adj_constants["ADJ_CHECKPOINT_STORAGE_MEMORY"]):
    for coeff in adj_variables.coeffs.keys(): 
      if adj_variables[coeff] == var: continue
      adjointer.record_variable(adj_variables[coeff], libadjoint.MemoryStorage(Vector(coeff), cs=True))

  elif cs == int(libadjoint.constants.adj_constants["ADJ_CHECKPOINT_STORAGE_DISK"]):
    for coeff in adj_variables.coeffs.keys(): 
      if adj_variables[coeff] == var: continue
      adjointer.record_variable(adj_variables[coeff], libadjoint.DiskStorage(Vector(coeff), cs=True))
  return linear, newargs

def solve(*args, **kwargs):
  '''This solve routine comes from the dolfin_adjoint package, and wraps the real Dolfin
  solve call. Its purpose is to annotate the model, recording what solves occur and what
  forms are involved, so that the adjoint model may be constructed automatically by libadjoint. 

  To disable the annotation, just pass annotate=False to this routine, and it acts exactly
  like the Dolfin solve call. This is useful in cases where the solve is known to be irrelevant
  or diagnostic for the purposes of the adjoint computation (such as projecting fields to
  other function spaces for the purposes of visualisation).'''

  # First, decide if we should annotate or not.
  to_annotate = True
  if "annotate" in kwargs:
    to_annotate = kwargs["annotate"]
    del kwargs["annotate"] # so we don't pass it on to the real solver

  if to_annotate:
    linear, newargs = annotate(*args, **kwargs)
  else:
    newargs = args

  ret = dolfin.fem.solving.solve(*newargs, **kwargs)

  if to_annotate:
    if not linear and debugging["fussy_replay"]:
      # we have annotated M.u = M.u - F(u),
      # but actually solved F(u) = 0.
      # so we need to do the mass solve too, so that the
      # replay is exact.
      nonlinear_post_solve_projection(*args, **kwargs)

    # Finally, if we want to record all of the solutions of the real forward model
    # (for comparison with a libadjoint replay later),
    # then we should record the value of the variable we just solved for.
    if debugging["record_all"]:
      if isinstance(args[0], ufl.classes.Equation):
        unpacked_args = dolfin.fem.solving._extract_args(*args, **kwargs)
        u  = unpacked_args[1]
        adjointer.record_variable(adj_variables[u], libadjoint.MemoryStorage(Vector(u)))
      elif isinstance(args[0], dolfin.cpp.Matrix):
        u = args[1]
        adjointer.record_variable(adj_variables[u], libadjoint.MemoryStorage(Vector(u)))
      else:
        raise libadjoint.exceptions.LibadjointErrorInvalidInputs("Don't know how to record, sorry")

  return ret

def adj_html(*args, **kwargs):
  '''This routine dumps the current state of the adjointer to a HTML visualisation.
  Use it like:
    adj_html("forward.html", "forward") # for the equations recorded on the forward run
    adj_html("adjoint.html", "adjoint") # for the equations to be assembled on the adjoint run
  '''
  return adjointer.to_html(*args, **kwargs)


class Vector(libadjoint.Vector):
  '''This class implements the libadjoint.Vector abstract base class for the Dolfin adjoint.
  In particular, it must implement the data callbacks for tasks such as adding two vectors
  together, duplicating vectors, taking norms, etc., that occur in the process of constructing
  the adjoint equations.'''

  def __init__(self, data, zero=False, fn_space=None):

    self.data=data
    # self.zero is true if we can prove that the vector is zero.
    if data is None:
      self.zero=True
    else:
      self.zero=zero

    if fn_space is not None:
      self.fn_space = fn_space

  def duplicate(self):

    if isinstance(self.data, ufl.form.Form):
      # The data type will be determined by the first addto.
      data = None
    elif isinstance(self.data, dolfin.Function):
      data = dolfin.Function(self.data.function_space())
    else:
      data = None

    fn_space = None
    if hasattr(self, "fn_space"):
      fn_space = self.fn_space

    return Vector(data, zero=True, fn_space=fn_space)

  def axpy(self, alpha, x):

    if x.zero:
      return

    if (self.data is None):
      # self is an empty form.
      assert(isinstance(x.data, ufl.form.Form))
      self.data=alpha*x.data
    elif isinstance(self.data, dolfin.Coefficient):
      if isinstance(x.data, dolfin.Coefficient):
        self.data.vector().axpy(alpha, x.data.vector())
      else:
        # This occurs when adding a RHS derivative to an adjoint equation
        # corresponding to the initial conditions.
        self.data.vector().axpy(alpha, dolfin.assemble(x.data))
    else:
      # self is a non-empty form.
      assert(isinstance(x.data, ufl.form.Form))
      assert(isinstance(self.data, ufl.form.Form))

      # Let's do a bit of argument shuffling, shall we?
      xargs = ufl.algorithms.extract_arguments(x.data)
      sargs = ufl.algorithms.extract_arguments(self.data)

      if xargs != sargs:
        # OK, let's check that all of the function spaces are happy and so on.
        for i in range(len(xargs)):
          assert xargs[i].element() == sargs[i].element()
          assert xargs[i].function_space() == sargs[i].function_space()

        # Now that we are happy, let's replace the xargs with the sargs ones.
        x_form = dolfin.replace(x.data, dict(zip(xargs, sargs)))
      else:
        x_form = x.data

      self.data+=alpha*x_form

    self.zero = False

  def norm(self):

    if isinstance(self.data, dolfin.Function):
      return (abs(dolfin.assemble(dolfin.inner(self.data, self.data)*dolfin.dx)))**0.5
    elif isinstance(self.data, ufl.form.Form):
      import scipy.linalg
      vec = dolfin.assemble(self.data)
      n = scipy.linalg.norm(vec)
      return n

  def dot_product(self,y):

    if isinstance(self.data, ufl.form.Form):
      return dolfin.assemble(dolfin.inner(self.data, y.data)*dolfin.dx)
    elif isinstance(self.data, dolfin.Function):
      import numpy
      if isinstance(y.data, ufl.form.Form):
        other = dolfin.assemble(y.data)
      else:
        other = y.data.vector()
      return numpy.dot(numpy.array(self.data.vector()), numpy.array(other))
    else:
      raise libadjoint.exceptions.LibadjointErrorNotImplemented("Don't know how to dot anything else.")

  def set_random(self):
    assert isinstance(self.data, dolfin.Function) or hasattr(self, "fn_space")
    import random

    if self.data is None:
      self.data = dolfin.Function(self.fn_space)

    vec = self.data.vector()
    for i in range(len(vec)):
      vec[i] = random.random()

    self.zero = False

  def size(self):
    if hasattr(self, "fn_space"):
      return self.fn_space.dim()

    if isinstance(self.data, dolfin.Function):
      return len(self.data.vector())

    if isinstance(self.data, ufl.form.Form):
      return len(dolfin.assemble(self.data))

    raise libadjoint.exceptions.LibadjointErrorNotImplemented("Don't know how to get the size.")

  def set_values(self, array):
    if isinstance(self.data, dolfin.Function):
      vec = self.data.vector()
      for i in range(len(array)):
        vec[i] = array[i]
      self.zero = False
    else:
      raise libadjoint.exceptions.LibadjointErrorNotImplemented("Don't know how to set values.")
  
  def write(self, var):
    import os.path

    filename = str(var)
    suffix = "xml"
    if not os.path.isfile(filename+".%s" % suffix):
      print "Warning: Overwritting checkpoint file "+filename+"."+suffix
    file = dolfin.File(filename+".%s" % suffix)
    file << self.data

    # Save the function space into checkpoint_fs. It will be needed when reading the variable back in.
    checkpoint_fs[filename] = self.data.function_space()

  @staticmethod
  def read(var):

    filename = str(var)
    suffix = "xml"

    V = checkpoint_fs[filename]
    v = dolfin.Function(V, filename+".%s" % suffix)
    return Vector(v)

  @staticmethod
  def delete(var):
    import os
    import os.path

    filename = str(var)
    suffix = "xml"

    assert(os.path.isfile(filename+".%s" % suffix))
    os.remove(filename+".%s" % suffix)

class Matrix(libadjoint.Matrix):
  '''This class implements the libadjoint.Matrix abstract base class for the Dolfin adjoint.
  In particular, it must implement the data callbacks for tasks such as adding two matrices
  together, duplicating matrices, etc., that occur in the process of constructing
  the adjoint equations.'''

  def __init__(self, data, bcs=None, ksp=None, pc=None):

    if bcs is None:
      self.bcs = []
    else:
      self.bcs=bcs

    self.data=data

    self.ksp = ksp
    self.pc = pc

  def solve(self, var, b):
      
    if isinstance(self.data, ufl.Identity):
      x=b.duplicate()
      x.axpy(1.0, b)
    else:
      if var.type in ['ADJ_TLM', 'ADJ_ADJOINT']:
        bcs = [dolfin.homogenize(bc) for bc in self.bcs if isinstance(bc, dolfin.DirichletBC)] + [bc for bc in self.bcs if not isinstance(bc, dolfin.DirichletBC)]
      else:
        bcs = self.bcs

      solver_parameters = {}
      if self.pc is not None:
        solver_parameters["preconditioner"] = self.pc
      if self.ksp is not None:
        solver_parameters["linear_solver"] = self.ksp

      x=Vector(dolfin.Function(self.test_function().function_space()))
      dolfin.fem.solving.solve(self.data==b.data, x.data, bcs, solver_parameters=solver_parameters)

    return x

  def axpy(self, alpha, x):
    assert isinstance(x.data, ufl.Form)
    assert isinstance(self.data, ufl.Form)

    # Let's do a bit of argument shuffling, shall we?
    xargs = ufl.algorithms.extract_arguments(x.data)
    sargs = ufl.algorithms.extract_arguments(self.data)

    if xargs != sargs:
      # OK, let's check that all of the function spaces are happy and so on.
      for i in range(len(xargs)):
        assert xargs[i].element() == sargs[i].element()
        assert xargs[i].function_space() == sargs[i].function_space()

      # Now that we are happy, let's replace the xargs with the sargs ones.
      x_form = dolfin.replace(x.data, dict(zip(xargs, sargs)))
    else:
      x_form = x.data

    self.data+=alpha*x_form
    self.bcs += x.bcs # Err, I hope they are compatible ...

  def test_function(self):
    '''test_function(self)

    Return the ufl.Argument corresponding to the trial space for the form'''

    return ufl.algorithms.extract_arguments(self.data)[-1]

class Functional(libadjoint.Functional):
  '''This class implements the libadjoint.Functional abstract base class for the Dolfin adjoint.
  It takes in a form, and implements the necessary routines such as calling the functional
  and taking its derivative.'''

  def __init__(self, form):

    self.form = form
    self.activated = False

  def __call__(self, dependencies, values):

    dolfin_dependencies=[dep for dep in ufl.algorithms.extract_coefficients(self.form) if hasattr(dep, "function_space")]

    dolfin_values=[val.data for val in values]

    return dolfin.assemble(dolfin.replace(self.form, dict(zip(dolfin_dependencies, dolfin_values))))

  def derivative(self, variable, dependencies, values):

    # Find the dolfin Function corresponding to variable.
    dolfin_variable = values[dependencies.index(variable)].data

    dolfin_dependencies = [dep for dep in ufl.algorithms.extract_coefficients(self.form) if hasattr(dep, "function_space")]

    dolfin_values = [val.data for val in values]

    current_form = dolfin.replace(self.form, dict(zip(dolfin_dependencies, dolfin_values)))
    test = dolfin.TestFunction(dolfin_variable.function_space())

    return Vector(dolfin.derivative(current_form, dolfin_variable, test))

  def dependencies(self, adjointer, timestep):

    if self.activated is False:
      deps = [adj_variables[coeff] for coeff in ufl.algorithms.extract_coefficients(self.form) if hasattr(coeff, "function_space")]      
      self.activated = True
    else:
      deps = []
    
    return deps

  def __str__(self):
    
    return hashlib.md5(str(self.form)).hexdigest()

class RHS(libadjoint.RHS):
  '''This class implements the libadjoint.RHS abstract base class for the Dolfin adjoint.
  It takes in a form, and implements the necessary routines such as calling the right-hand side
  and taking its derivative.'''
  def __init__(self, form):

    self.form=form

    if isinstance(self.form, ufl.form.Form):
      self.deps = [adj_variables[coeff] for coeff in ufl.algorithms.extract_coefficients(self.form) if hasattr(coeff, "function_space")]      
    else:
      self.deps = []

    if isinstance(self.form, ufl.form.Form):
      self.coeffs = [coeff for coeff in ufl.algorithms.extract_coefficients(self.form) if hasattr(coeff, "function_space")]      
    else:
      self.coeffs = []

  def __call__(self, dependencies, values):

    if isinstance(self.form, ufl.form.Form):

      dolfin_dependencies=[dep for dep in ufl.algorithms.extract_coefficients(self.form) if hasattr(dep, "function_space")]

      dolfin_values=[val.data for val in values]

      return Vector(dolfin.replace(self.form, dict(zip(dolfin_dependencies, dolfin_values))))

    else:
      # RHS is a Vector.
      assert isinstance(self.form, Vector)
      return self.form
    

  def derivative_action(self, dependencies, values, variable, contraction_vector, hermitian):

    if isinstance(self.form, ufl.form.Form):
      # Find the dolfin Function corresponding to variable.
      dolfin_variable = values[dependencies.index(variable)].data

      dolfin_dependencies = [dep for dep in ufl.algorithms.extract_coefficients(self.form) if hasattr(dep, "function_space")]

      dolfin_values = [val.data for val in values]

      current_form = dolfin.replace(self.form, dict(zip(dolfin_dependencies, dolfin_values)))
      trial = dolfin.TrialFunction(dolfin_variable.function_space())

      d_rhs=dolfin.derivative(current_form, dolfin_variable, trial)

      if hermitian:
        action = dolfin.action(dolfin.adjoint(d_rhs),contraction_vector.data)
      else:
        action = dolfin.action(d_rhs,contraction_vector.data)

      return Vector(action)
    else:
      # RHS is a Vector. Its derivative is therefore zero.
      raise exceptions.LibadjointErrorNotImplemented("No derivative method for constant RHS.")


  def dependencies(self):

    return self.deps

  def coefficients(self):

    return self.coeffs

  def __str__(self):
    
    return hashlib.md5(str(self.form)).hexdigest()

class NonlinearRHS(RHS):
  '''For nonlinear problems, the source term isn't assembled in the usual way.
  If the nonlinear problem is given as
  F(u) = 0,
  we annotate it as
  M.u = M.u - F(u) .
  So in order to actually assemble the right-hand side term,
  we first need to solve F(u) = 0 to find the specific u,
  and then multiply that by the mass matrix.'''
  def __init__(self, form, F, u, bcs, mass):
    '''form is M.u - F(u). F is the nonlinear equation, F(u) := 0.'''
    RHS.__init__(self, form)
    self.F = F
    self.u = u
    self.bcs = bcs
    self.mass = mass

    # We want to mark that the RHS term /also/ depends on
    # the previous value of u, as that's what we need to initialise
    # the nonlinear solver.
    var = adj_variables[self.u]
    if var.timestep > 0 and debugging["fussy_replay"]:
      var.c_object.timestep = var.c_object.timestep - 1
      self.deps.append(var)
      self.ic_var = var
    else:
      self.ic_var = None

  def __call__(self, dependencies, values):
    assert isinstance(self.form, ufl.form.Form)

    ic = self.u.function_space() # by default, initialise with a blank function in the solution FunctionSpace
    replace_map = {}

    for i in range(len(self.deps)):
      if self.deps[i] in dependencies:
        j = dependencies.index(self.deps[i])
        if self.deps[i] == self.ic_var:
          ic = values[j].data # ahah, we have found an initial condition!
        else:
          replace_map[self.coeffs[i]] = values[j].data

    current_F    = dolfin.replace(self.F, replace_map)
    u = dolfin.Function(ic)
    current_F    = dolfin.replace(current_F, {self.u: u})

    # OK, here goes nothing:
    dolfin.solve(current_F == 0, u, self.bcs)

    return Vector(dolfin.action(self.mass, u))

  def derivative_action(self, dependencies, values, variable, contraction_vector, hermitian):
    '''If variable is the variable for the initial condition, we want to ignore it,
    and set the derivative to zero. Assuming the solver converges, the sensitivity of
    the solution to the initial condition should be extremely small, and computing it
    is very difficult (one would have to do a little adjoint solve to compute it).
    Even I'm not that fussy.'''

    if variable == self.ic_var:
      deriv_value = values[dependencies.index(variable)].data
      return Vector(None, fn_space=deriv_value.function_space())
    else:
      return RHS.derivative_action(self, dependencies, values, variable, contraction_vector, hermitian)

  def derivative_assembly(self, dependencies, values, variable, hermitian):
    replace_map = {}

    for i in range(len(self.deps)):
      if self.deps[i] == self.ic_var: continue
      j = dependencies.index(self.deps[i])
      replace_map[self.coeffs[i]] = values[j].data

    diff_var = values[dependencies.index(variable)].data

    current_form = dolfin.replace(self.form, replace_map)
    deriv = dolfin.derivative(current_form, diff_var)

    if hermitian:
      deriv = dolfin.adjoint(deriv)
      bcs = [dolfin.homogenize(bc) for bc in self.bcs if isinstance(bc, dolfin.DirichletBC)]
    else:
      bcs = self.bcs

    return Matrix(deriv, bcs=bcs)

dolfin_assign = dolfin.Function.assign
def dolfin_adjoint_assign(self, other, annotate=True):
  '''We also need to monkeypatch the Function.assign method, as it is often used inside 
  the main time loop, and not annotating it means you get the adjoint wrong for totally
  nonobvious reasons. If anyone objects to me monkeypatching your objects, my apologies
  in advance.'''

  # ignore anything not a dolfin.Function
  if not isinstance(other, dolfin.Function) or annotate is False:
    return dolfin_assign(self, other)

  # ignore anything that is an interpolation, rather than a straight assignment
  if self.function_space() != other.function_space():
    return dolfin_assign(self, other)

  other_var = adj_variables[other]
  # ignore any functions we haven't seen before -- we DON'T want to
  # annotate the assignment of initial conditions here. That happens
  # in the main solve wrapper.
  if not adjointer.variable_known(other_var):
    return dolfin_assign(self, other)

  # OK, so we have a variable we've seen before. Beautiful.
  fn_space = other.function_space()
  u, v = dolfin.TestFunction(fn_space), dolfin.TrialFunction(fn_space)
  M = dolfin.inner(u, v)*dolfin.dx
  if debugging["fussy_replay"]:
    return solve(M == dolfin.action(M, other), self) # this takes care of all the annotation etc
  else:
    annotate(M == dolfin.action(M, other), self)
    return dolfin_assign(self, other)

dolfin.Function.assign = dolfin_adjoint_assign

def define_nonlinear_equation(F, u):
  # Given an F := 0,
  # we write the equation for libadjoint's annotation purposes as
  # M.u = M.u + F(u)
  # as we need to have something on the diagonal in our big time system

  fn_space = u.function_space()
  test = dolfin.TestFunction(fn_space)
  trial = dolfin.TrialFunction(fn_space)

  mass = dolfin.inner(test, trial)*dolfin.dx

  return (mass, dolfin.action(mass, u) - F)

def nonlinear_post_solve_projection(*args, **kwargs):
  '''we have annotated M.u = M.u - F(u),
  but actually solved F(u) = 0.
  so we need to do the mass solve too, so that the
  replay is exact.'''

  # Unpack the arguments, using the same routine as the real Dolfin solve call
  unpacked_args = dolfin.fem.solving._extract_args(*args, **kwargs)
  u  = unpacked_args[1]

  fn_space = u.function_space()
  test = dolfin.TestFunction(fn_space)
  trial = dolfin.TrialFunction(fn_space)

  mass = dolfin.inner(test, trial)*dolfin.dx

  dolfin.fem.solving.solve(mass == dolfin.action(mass, u), u)

def adjoint_checkpointing(strategy, steps, snaps_on_disk, snaps_in_ram, verbose=False):
  adjointer.set_checkpoint_strategy(strategy)
  adjointer.set_revolve_options(steps, snaps_on_disk, snaps_in_ram, verbose)

class InitialConditionParameter(libadjoint.Parameter):
  '''This Parameter is used as input to the tangent linear model (TLM)
  when one wishes to compute dJ/d(initial condition) in a particular direction (perturbation).'''
  def __init__(self, coeff, perturbation):
    '''coeff: the variable whose initial condition you wish to perturb.
       perturbation: the perturbation direction in which you wish to compute the gradient. Must be a Function.'''
    self.var = adj_variables[coeff]
    self.var.c_object.timestep = 0 # we want to put in the source term only at the initial condition.
    self.perturbation = Vector(perturbation).duplicate()
    self.perturbation.axpy(1.0, Vector(perturbation))

  def __call__(self, dependencies, values, variable):
    # The TLM source term only kicks in at the start, for the initial condition:
    if self.var == variable:
      return self.perturbation
    else:
      return None

  def __str__(self):
    return self.var.name + ':InitialCondition'
