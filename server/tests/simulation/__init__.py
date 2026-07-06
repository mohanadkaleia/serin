"""ENG-71 — M1 simulation suite (the §12 convergence acceptance harness).

Package marker.  Its presence makes pytest treat ``simulation`` as a package and
insert ``server/tests`` (not this directory) onto ``sys.path`` — so the modules
here reach the shared ``authutil`` / ``eventsutil`` helpers exactly as the sibling
integration tests do, while intra-suite imports use the ``simulation.*`` package
path.

See ``test_simulation.py``'s module docstring for the M1-vs-M2 seam map.
"""
