#!/usr/bin/python

import os
import virtualenv

os.chdir(os.path.dirname(__file__))
output = virtualenv.create_bootstrap_script('')
with open('venv-bootstrap.py', 'w') as fd:
  fd.write(output)

