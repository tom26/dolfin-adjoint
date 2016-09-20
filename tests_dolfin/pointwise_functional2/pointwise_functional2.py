from dolfin import *
from dolfin_adjoint import *
import ufl

import numpy as np

# Define mesh
mesh = UnitSquareMesh(10, 10)
n    = FacetNormal(mesh)
U    = FunctionSpace(mesh, "CG", 1)

adj_start_timestep()

def forward(c):
    u  = Function(U)
    u0 = Function(U)
    v  = TestFunction(U)
    F  = inner(u - u0 - c, v)*dx
    for t in range(1, 5):
        solve(F == 0, u)
        u0.assign(u)
        adj_inc_timestep(t, t == 4)
    return u0

c = Constant(3.)
u = forward(c)

# check for one coord
J = PointwiseFunctional(u, [0, 0, 0, 0], Point(np.array([0.4, 0.4])), [1, 2, 3, 4], u_ind=[None])
Jr = ReducedFunctional(J, Control(c))

Jr3 = Jr(Constant(3))

# Check for multiple coords
mJ = PointwiseFunctional(u, [[0, 0, 0, 0], [1, 1, 1, 1]], [Point(np.array([0.2, 0.2])), Point(np.array([0.4, 0.4]))], [1, 2, 3, 4], u_ind=[None, None])
mJr = ReducedFunctional(mJ, Control(c))

mJr3 = mJr(Constant(3))


assert Jr.taylor_test(Constant(5)) > 1.9
assert mJr.taylor_test(Constant(5)) > 1.9
assert abs(Jr3 - 270.0) < 1e-12
assert abs(mJr3 - 484.0) < 1e-12
