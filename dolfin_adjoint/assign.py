import dolfin
import ufl
from solving import solve, annotate as solving_annotate, do_checkpoint
import libadjoint
import adjlinalg
import adjglobals

def register_assign(new, old):
  fn_space = new.function_space()
  block_name = "Identity: %s" % str(fn_space)
  if len(block_name) > int(libadjoint.constants.adj_constants["ADJ_NAME_LEN"]):
    block_name = block_name[0:int(libadjoint.constants.adj_constants["ADJ_NAME_LEN"])-1]
  identity_block = libadjoint.Block(block_name)

  def identity_assembly_cb(variables, dependencies, hermitian, coefficient, context):
    assert coefficient == 1
    return (adjlinalg.Matrix(adjlinalg.IdentityMatrix()), adjlinalg.Vector(dolfin.Function(fn_space)))

  identity_block.assemble = identity_assembly_cb
  dep = adjglobals.adj_variables.next(new)

  if dolfin.parameters["adjoint"]["record_all"]:
    adjglobals.adjointer.record_variable(dep, libadjoint.MemoryStorage(adjlinalg.Vector(old)))

  initial_eq = libadjoint.Equation(dep, blocks=[identity_block], targets=[dep], rhs=IdentityRHS(old))
  cs = adjglobals.adjointer.register_equation(initial_eq)

  do_checkpoint(cs, dep)

class IdentityRHS(libadjoint.RHS):
  def __init__(self, var):
    self.var = var
    self.dep = adjglobals.adj_variables[var]

  def __call__(self, dependencies, values):
    return adjlinalg.Vector(values[0].data)

  def derivative_action(self, dependencies, values, variable, contraction_vector, hermitian):
    return adjlinalg.Vector(contraction_vector.data)

  def dependencies(self):
    return [self.dep]

  def coefficients(self):
    return [self.var]

  def __str__(self):
    return "AssignIdentity(%s)" % str(self.dep)
