# Builds a Docker image with dolfin-adjoint stable version built from
# git sources. The image is at:
#
#    https://quay.io/repository/dolfinadjoint/dolfin-adjoint
#
# Authors:
# Jack S. Hale <jack.hale@uni.lu>
# Simon W. Funke <simon@simula.no>

FROM quay.io/fenicsproject/stable:2017.1.0.r1
MAINTAINER Simon W. Funke <simon@simula.no>

USER root
RUN apt-get -qq update && \
    apt-get -y install python-scipy emacs-nox nano gdb libjsoncpp-dev && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

USER fenics
ENV DOLFIN_ADJOINT_VERSION 2017.1.0
ENV IPOPT_VER=3.12.6

COPY dolfin-adjoint.conf $FENICS_HOME/dolfin-adjoint.conf
RUN /bin/bash -l -c "source $FENICS_HOME/dolfin-adjoint.conf && \
                     update_libadjoint && \
                     update_dolfin-adjoint && \
                     update_moola && \
                     update_ipopt && \
                     update_pyipopt && \
                     update_optizelle && \
                     rm -rf $FENICS_HOME/build/src"

COPY WELCOME $FENICS_HOME/WELCOME
RUN echo "source $FENICS_HOME/dolfin-adjoint.conf" >> $FENICS_HOME/.bash_profile

USER root
