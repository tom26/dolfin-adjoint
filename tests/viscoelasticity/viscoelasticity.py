__author__ = "Marie E. Rognes (meg@simula.no)"
__copyright__ = "Copyright (C) 2012 Marie Rognes"
__license__  = "Distribute at will"

"""
Schematic drawing (starts with 1 springs, starts with 0 dashpots)

      | A10 --- A00 |
----- |             | --------
      |     A11     |

Standard linear solid (SLS) viscoelastic model:

  A_E^0 \dot \sigma_0 + A_V^0 \sigma_0 = strain(u)
  A_E^1 \dot \sigma_1 = strain(v)

  \sigma = \sigma_0 + \sigma_1

  \div \sigma = gx
  \skew \sigma = 0

NB: Mesh in mm, remember that Pa = N/m^2 = kg/(m s^2) = g/(mm s^2)
Give bc and Lame parameters in kPa -> displacements in mm, velocities
in mm/s, stresses in kPa
"""

import sys
import pylab

from dolfin import *
from dolfin import div as d

# Adjoint stuff
from dolfin_adjoint import *
import libadjoint

penalty_beta = 10**8 # NB: Sensitive to this for values less than 10^6
dirname = "test-results"

mu00 = Constant(3.7466 * 10)
lamda00 = Constant(10**4)
mu10 = Constant(4.158)
lamda10 = Constant(10**3) # kPa
mu11 = Constant(2.39) # kPa
lamda11 = Constant(10**3) # kPa
params = [mu00, lamda00, mu10, lamda10, mu11, lamda11]

# Vectorized div
def div(v):
    return as_vector((d(v[0]), d(v[1]), d(v[2])))

# Vectorized skew
def skw(tau):
    s = 2*skew(tau) # FIXME: Why did I put a 2 here?
    return as_vector((s[0][1], s[0][2], s[1][2]))

# Compliance tensors (Semi-arbitrarily chosen values and units)
def A00_tensor(tau, mu, lamda):
    "Maxwell dashpot (eta)"
    foo = 1.0/(2*mu)*(tau - lamda/(2*mu + 3*lamda)*tr(tau)*Identity(3))
    return foo

def A10_tensor(tau, mu, lamda):
    "Maxwell spring (A2)"
    foo = 1.0/(2*mu)*(tau - lamda/(2*mu + 3*lamda)*tr(tau)*Identity(3))
    return foo

def A11_tensor(tau, mu, lamda):
    "Elastic spring (A1)"
    foo = 1.0/(2*mu)*(tau - lamda/(2*mu + 3*lamda)*tr(tau)*Identity(3))
    return foo

def get_box():
    "Use this for simple testing."
    n = 1
    mesh = Box(0., 0., 0., 20., 20., 100., 2*n, 2*n, 10*n)

    # Mark all facets by 0, exterior facets by 1, and then top and
    # bottom by 2
    boundaries = FacetFunction("uint", mesh)
    boundaries.set_all(0)
    on_bdry = AutoSubDomain(lambda x, on_boundary: on_boundary)
    top = AutoSubDomain(lambda x, on_boundary: near(x[2], 100.))
    bottom = AutoSubDomain(lambda x, on_boundary: near(x[2], 0.0))
    on_bdry.mark(boundaries, 1)
    top.mark(boundaries, 2)
    bottom.mark(boundaries, 2)
    return (mesh, boundaries)

def get_spinal_cord():
    "Mesh generated by Martin Alnaes using VMTK"
    #mesh = Mesh("mesh_edgelength4.xml.gz") # Coarse mesh
    mesh = Mesh("mesh_edgelength2.xml.gz")

    boundaries = mesh.domains().facet_domains(mesh)
    for (i, a) in enumerate(boundaries.array()):
        if a > 10:
            boundaries.array()[i] = 0
        if a == 3:
            boundaries.array()[i] = 2
    return (mesh, boundaries)

def crank_nicolson_step(Z, z_, k_n, g, v_D_mid, ds, params):

    # Define trial and test functions
    (sigma0, sigma1, v, gamma) = TrialFunctions(Z)
    (tau0, tau1, w, eta) = TestFunctions(Z)

    # Extract previous components
    (sigma0_, sigma1_, v_, gamma_) = split(z_)

    # Define midpoint values for brevity
    def avg(q, q_):
        return 0.5*(q + q_)
    sigma0_mid = avg(sigma0, sigma0_)
    sigma1_mid = avg(sigma1, sigma1_)
    v_mid = avg(v, v_)
    gamma_mid = avg(gamma, gamma_)

    # Define short-hand for compliance tensors to increase readability
    A00 = lambda sigma: A00_tensor(sigma, params[0], params[1])
    A10 = lambda sigma: A10_tensor(sigma, params[2], params[3])
    A11 = lambda sigma: A11_tensor(sigma, params[4], params[5])

    # Define form
    n = FacetNormal(Z.mesh())
    F = (inner(inv(k_n)*A10(sigma0 - sigma0_), tau0)*dx
         + inner(A00(sigma0_mid), tau0)*dx
         + inner(inv(k_n)*A11(sigma1 - sigma1_), tau1)*dx
         + inner(div(tau0 + tau1), v_mid)*dx
         + inner(skw(tau0 + tau1), gamma_mid)*dx
         + inner(div(sigma0_mid + sigma1_mid), w)*dx
         + inner(skw(sigma0_mid + sigma1_mid), eta)*dx
         - inner(0.5*v_, (tau0 + tau1)*n)*ds(1)
         - inner(v_D_mid, (tau0 + tau1)*n)*ds(2) # Velocity on dO_D
         )

    # Tricky to enforce Dirichlet boundary conditions on varying sums
    # of components (same deal as for slip for Stokes for
    # instance). Use penalty instead
    beta = Constant(penalty_beta)
    h = tetrahedron.volume
    F_penalty = 0.5*(beta*inv(h)*inner((tau0 + tau1)*n,
                                       (sigma0 + sigma1)*n - g)*ds(1))
    F = F + F_penalty

    return F

def bdf2_step(Z, z_, z__, k_n, g, v_D, ds, params):

    # Define trial and test functions
    (sigma0, sigma1, v, gamma) = TrialFunctions(Z)
    (tau0, tau1, w, eta) = TestFunctions(Z)

    # Extract previous components
    (sigma0_, sigma1_, v_, gamma_) = split(z_)
    (sigma0__, sigma1__, v__, gamma__) = split(z__)

    # Define short-hand for compliance tensors to increase readability
    A00 = lambda sigma: A00_tensor(sigma, params[0], params[1])
    A10 = lambda sigma: A10_tensor(sigma, params[2], params[3])
    A11 = lambda sigma: A11_tensor(sigma, params[4], params[5])

    # Define complete form
    n = FacetNormal(Z.mesh())
    F = (inner(inv(k_n)*A10(1.5*sigma0 - 2.*sigma0_ + 0.5*sigma0__), tau0)*dx
         + inner(A00(sigma0), tau0)*dx
         + inner(inv(k_n)*A11(1.5*sigma1 - 2.*sigma1_ + 0.5*sigma1__), tau1)*dx
         + inner(div(tau0 + tau1), v)*dx
         + inner(skw(tau0 + tau1), gamma)*dx
         + inner(div(sigma0 + sigma1), w)*dx
         + inner(skw(sigma0 + sigma1), eta)*dx
         - inner(v_D, (tau0 + tau1)*n)*ds(2)
         )

    # Enforce essential bc on stress by penalty
    beta = Constant(penalty_beta)
    h = tetrahedron.volume
    F_penalty = beta*inv(h)*inner((tau0 + tau1)*n,
                                  (sigma0 + sigma1)*n - g)*ds(1)
    F = F + F_penalty
    return F

# Quick testing for box:
(mesh, boundaries) = get_box()
p = Expression("sin(2*pi*t)*1.0/(100)*x[2]", t=0)
amplitude = Constant(0.05)

# Semi-realistic stuff:
#(mesh, boundaries) = get_spinal_cord()
#p = Expression("0.05*sin(2*pi*t)*(1.0/(171 - 78)*(x[2] - 78))", t=0)  # kPa
#amplitude = Constant(0.05)

# Define function spaces
S = VectorFunctionSpace(mesh, "BDM", 1)
V = VectorFunctionSpace(mesh, "DG", 0)
Q = VectorFunctionSpace(mesh, "DG", 0)
CG1 = VectorFunctionSpace(mesh, "CG", 1)
Z = MixedFunctionSpace([S, S, V, Q])

def main(ic, params, amplitude, T=1.0, dt=0.01, annotate=False):
    # dk = half the timestep
    dk = dt/2.0

    parameters["form_compiler"]["optimize"] = True
    parameters["form_compiler"]["cpp_optimize"] = True

    ds = Measure("ds")[boundaries]

    # Define functions for previous timestep (z_), half-time (z_star)
    # and current (z)
    z_ = Function(ic)
    z_star = Function(Z)
    z = Function(Z)

    # Boundary conditions
    v_D_mid = Function(V) # Velocity condition at half time
    v_D = Function(V)     # Velocity condition at time

    # Boundary traction (pressure originating from CSF flow)
    n = FacetNormal(mesh)
    g = - amplitude*p*n

    F_cn = crank_nicolson_step(Z, z_, Constant(dk), g, v_D_mid, ds, params)
    (a_cn, L_cn) = system(F_cn)
    A_cn = assemble(a_cn)
    cn_solver = LUSolver(A_cn)
    cn_solver.parameters["reuse_factorization"] = True

    F_bdf = bdf2_step(Z, z_star, z_, Constant(dk), g, v_D, ds, params)
    (a_bdf, L_bdf) = system(F_bdf)
    A_bdf = assemble(a_bdf)
    bdf_solver = LUSolver(A_bdf)
    bdf_solver.parameters["reuse_factorization"] = True

    file = File("%s/forward_%d_%g.xml" % (dirname, 0, 0.0))
    file << ic

    progress = Progress("Time-iteration", int(T/dt))
    t = dk
    iteration = 1
    while (t <= T):

        # Half-time step:
        # Update source(s)
        p.t = t

        # Assemble right-hand side for CN system
        b = assemble(L_cn)

        # Solve Crank-Nicolson system
        cn_solver.solve(z_star.vector(), b, annotate=annotate)

        # Increase time
        t += dk

        # Next-time step:
        # Update sources
        p.t = t

        # Assemble right-hand side for BDF system
        b = assemble(L_bdf)

        # Solve BDF system
        bdf_solver.solve(z.vector(), b, annotate=annotate)

        # Store solutions
        file = File("%s/forward_%d_%g.xml" % (dirname, iteration, t))
        file << z

        # Update time and variables
        t += dk
        z_.assign(z)

        progress += 1
        iteration += 1

    return z_

if __name__ == "__main__":

    # Adjust behaviour at will:
    T = 0.05
    dt = 0.01
    set_log_level(PROGRESS)

    dolfin.parameters["adjoint"]["record_all"] = True
    ic = Function(Z)
    ic_copy = Function(ic)

    # Play forward run
    info_blue("Running forward ... ")
    z = main(ic, params, amplitude, T=T, dt=dt, annotate=True)

    # Replay forward
    info_blue("Replaying forward run ... ")
    adj_html("forward.html", "forward")
    replay_dolfin(forget=False)

    # Use elastic/viscous traction on vertical plane as goal
    (sigma0, sigma1, v, gamma) = split(z)
    sigma = sigma0 + sigma1
    J = FinalFunctional(inner(sigma0[2], sigma0[2])*dx)
    param = ScalarParameter(amplitude)
    adjointer = adjglobals.adjointer

# Copy the code from compute_gradient:
    #dJdp = compute_gradient(J, ScalarParameter(amplitude))
# as we want to also dump out norms of the adjoint solution.
#   --------------------------------------------------------------
    norm0s = []
    norm1s = []
    dJdparam = None

    for i in range(adjointer.equation_count)[::-1]:
      (adj_var, output) = adjointer.get_adjoint_solution(i, J)

      storage = libadjoint.MemoryStorage(output)
      adjointer.record_variable(adj_var, storage)
      fwd_var = libadjoint.Variable(adj_var.name, adj_var.timestep, adj_var.iteration)

      out = param.inner_adjoint(adjointer, output.data, i, fwd_var)
      if dJdparam is None:
        dJdparam = out
      elif dJdparam is not None and out is not None:
        dJdparam += out

      if adj_var.name == "w_{10}":
        (adj_sigma0, adj_sigma1, adj_v, adj_gamma) = output.data.split()
        norm0s += [norm(adj_sigma0)]
        norm1s += [norm(adj_sigma1)]

      adjointer.forget_adjoint_equation(i)
    dJdp = dJdparam
#   --------------------------------------------------------------

    print "dJ/dp: ", dJdp
    #print "norm0s: ", norm0s
    #print "norm1s: ", norm1s

    def Jfunc(amplitude):
      ic.vector()[:] = ic_copy.vector()
      z = main(ic, params, amplitude, T=T, dt=dt, annotate=False)
      (sigma0, sigma1, v, gamma) = split(z)
      sigma = sigma0 + sigma1
      J = assemble(inner(sigma0[2], sigma0[2])*dx)
      print "J(.): ", J
      return J

    info_blue("Checking adjoint correctness ... ")
    minconv = test_scalar_parameter_adjoint(Jfunc, amplitude, dJdp)

    if minconv < 1.8:
      sys.exit(1)

