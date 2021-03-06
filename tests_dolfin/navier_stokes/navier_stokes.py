"""This demo program solves the incompressible Navier-Stokes equations
on an L-shaped domain using Chorin's splitting method."""

# Copyright (C) 2010-2011 Anders Logg
#
# This file is part of DOLFIN.
#
# DOLFIN is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# DOLFIN is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with DOLFIN. If not, see <http://www.gnu.org/licenses/>.
#
# Modified by Mikael Mortensen 2011
#
# First added:  2010-08-30
# Last changed: 2011-06-30

# Begin demo

from dolfin import *
from dolfin_adjoint import *

# Print log messages only from the root process in parallel
parameters["std_out_all_processes"] = False;

#parameters["mesh_partitioner"] = "SCOTCH";

# Load mesh from file
#mesh = Mesh("lshape.xml.gz")
mesh = UnitSquareMesh(2, 2)

# Define function spaces (P2-P1)
cg2 = VectorElement("CG", triangle, 2)
cg1 = FiniteElement("CG", triangle, 1)
V = FunctionSpace(mesh, cg2)
Q = FunctionSpace(mesh, cg1)

# Define trial and test functions
u = TrialFunction(V)
p = TrialFunction(Q)
v = TestFunction(V)
q = TestFunction(Q)

def main(ic):
    # Set parameter values
    dt = 0.01
    T = 0.05
    nu = 0.01

    # Define time-dependent pressure boundary condition
    p_in = Expression("sin(3.0*t)", t=0.0, degree=1)

    # Define boundary conditions
    noslip  = DirichletBC(V, (0, 0),
                          "on_boundary && \
                           (x[0] < DOLFIN_EPS | x[1] < DOLFIN_EPS | \
                           (x[0] > 0.5 - DOLFIN_EPS && x[1] > 0.5 - DOLFIN_EPS))")
    inflow  = DirichletBC(Q, p_in, "x[1] > 1.0 - DOLFIN_EPS")
    outflow = DirichletBC(Q, 0, "x[0] > 1.0 - DOLFIN_EPS")
    bcu = [noslip]
    bcp = [inflow, outflow]

    # Create functions
    u0 = ic.copy(deepcopy=True, name="Velocity")
    u1 = Function(V, name="VelocityNext")
    p1 = Function(Q, name="Pressure")

    # Define coefficients
    k = Constant(dt)
    f = Constant((0, 0))

    # Tentative velocity step
    F1 = (1/k)*inner(u - u0, v)*dx + inner(grad(u0)*u0, v)*dx + \
         nu*inner(grad(u), grad(v))*dx - inner(f, v)*dx
    a1 = lhs(F1)
    L1 = rhs(F1)

    # Pressure update
    a2 = inner(grad(p), grad(q))*dx
    L2 = -(1/k)*div(u1)*q*dx

    # Velocity update
    a3 = inner(u, v)*dx
    L3 = inner(u1, v)*dx - k*inner(grad(p1), v)*dx

    # Assemble matrices
    A1 = assemble(a1)
    A2 = assemble(a2)
    A3 = assemble(a3)

    prec = "amg" if has_krylov_solver_preconditioner("amg") else "default"

    begin("Projecting initial velocity")
    phi = Function(Q, name = "ScalarPotential")
    b = assemble(-div(u0) * q * dx)
    [bc.apply(A2, b) for bc in bcp]
    solve(A2, phi.vector(), b, "gmres", prec)
    b = assemble(inner(u0, v) * dx - inner(grad(phi), v) * dx)
    [bc.apply(A3, b) for bc in bcu]
    solve(A3, u0.vector(), b, "gmres", "default")
    del(phi, b)
    end()

    # Time-stepping
    t = dt
    while t < T + DOLFIN_EPS:

        # Update pressure boundary condition
        p_in.t = t

        # Compute tentative velocity step
        begin("Computing tentative velocity")
        b1 = assemble(L1)
        [bc.apply(A1, b1) for bc in bcu]
        solve(A1, u1.vector(), b1, "gmres", "default")
        end()

        # Pressure correction
        begin("Computing pressure correction")
        b2 = assemble(L2)
        [bc.apply(A2, b2) for bc in bcp]
        solve(A2, p1.vector(), b2, "gmres", prec)
        end()

        # Velocity correction
        begin("Computing velocity correction")
        b3 = assemble(L3)
        [bc.apply(A3, b3) for bc in bcu]
        solve(A3, u1.vector(), b3, "gmres", "default")
        end()

        # Move to next time step
        u0.assign(u1)
        t += dt
        adj_inc_timestep()
    return u0

if __name__ == "__main__":

    import sys

    ic = Function(V)
    soln = main(ic)
    parameters["adjoint"]["stop_annotating"] = True

    J = Functional(inner(soln, soln)**1*dx*dt[FINISH_TIME] + inner(soln, soln)*dx*dt[START_TIME])
    m = Control(soln)
    Jm = assemble(inner(soln, soln)**1*dx + inner(ic, ic)*dx)
    rf = ReducedFunctional(J, m)
    dJdm = rf.derivative(forget=False)[0]
    HJm  = lambda m_dot: rf.hessian(m_dot)

    def J(ic):
        soln = main(ic)
        return assemble(inner(soln, soln)*dx + inner(ic, ic)*dx)

    perturbation_direction=interpolate(Constant((1.0, 1.0)), V)
    minconv = taylor_test(J, m, Jm, dJdm, HJm=HJm, perturbation_direction=perturbation_direction)
    assert minconv > 2.7
