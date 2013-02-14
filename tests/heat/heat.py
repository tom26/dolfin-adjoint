import sys

from dolfin import *
from dolfin_adjoint import *

f = Expression("x[0]*(x[0]-1)*x[1]*(x[1]-1)")
mesh = UnitSquareMesh(4, 4)
V = FunctionSpace(mesh, "CG", 1)

def main(ic, annotate=True):
  u = TrialFunction(V)
  v = TestFunction(V)

  u_0 = Function(V, name="Solution")
  u_0.assign(ic, annotate=False)

  u_1 = Function(V, name="NextSolution")

  dt = Constant(0.1)

  F = ( (u - u_0)/dt*v + inner(grad(u), grad(v)) + f*v)*dx

  bc = DirichletBC(V, 1.0, "on_boundary")

  a, L = lhs(F), rhs(F)

  t = float(dt)
  T = 1.0
  n = 1

  while t <= T:

      solve(a == L, u_0, bc, annotate=annotate)
      t += float(dt)

  return u_0

if __name__ == "__main__":

  ic = Function(V, name="InitialCondition")
  u = main(ic)

  adj_html("forward.html", "forward")
  adj_html("adjoint.html", "adjoint")

  J = Functional(u*u*dx*dt[FINISH_TIME])
  m = InitialConditionParameter(u)
  Jm = assemble(u*u*dx)
  dJdm = compute_gradient(J, m, forget=False)
  HJm  = hessian(J, m)

  def J(ic):
    u = main(ic, annotate=False)
    return assemble(u*u*dx)

  minconv = taylor_test(J, m, Jm, dJdm, seed=10.0)
  assert minconv > 1.9
