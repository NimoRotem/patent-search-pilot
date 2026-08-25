"""Turning what the applicant supplied into geometry this pipeline owns.

Everything here reads an authoritative source and produces the same two things the rest of the
compiler speaks: a ``solid.Mesh`` for anything three dimensional, or a list of polylines in
millimetres for anything flat. Nothing here asks a model anything. An importer that guessed would
put invention back into the one place the product exists to keep it out of.
"""
