from dolfin import *
from dolfin_adjoint import *
import sys

n = 30
mesh = UnitInterval(n)
V = FunctionSpace(mesh, "CG", 2)

def main(ic, nu):
  u = Function(ic)
  u_next = Function(V)
  v = TestFunction(V)


  timestep = Constant(1.0/n)

  F = ((u_next - u)/timestep*v
       + u_next*grad(u_next)*v + nu*grad(u_next)*grad(v))*dx
  bc = DirichletBC(V, 0.0, "on_boundary")

  t = 0.0
  end = 0.2
  while (t <= end):
      solve(F == 0, u_next, bc)
      u.assign(u_next)
      t += float(timestep)

  return u

if __name__ == "__main__":
  ic = project(Expression("sin(2*pi*x[0])"),  V)
  nu = Constant(0.0001)

  u = main(ic, nu)

  J = FinalFunctional(inner(u, u)*dx)
  dJdnu = compute_gradient(J, ScalarParameter(nu))

  def Jhat(nu):
    u = main(ic, nu)
    return assemble(inner(u, u)*dx)

  minconv = test_scalar_parameter_adjoint(Jhat, nu, dJdnu)

  if minconv < 1.9:
    sys.exit(1)