About
=====

Autodeps automatically builds a virtualenv environment based on
requirements.txt. This build happens on demand when autodeps is
imported.

Usage
=====

First, add autodeps to your project. Either using a submodule or by
copying it.

Next, add something similar to the following in your ``adddeps.py`` or
at the top of your main file:

.. code-block:: python

    root = os.path.dirname(os.path.abspath(__file__))
    autodeps = os.path.join(root, '../autodeps/autodeps.py')
    execfile(autodeps, {'__file__': autodeps,
                        '__name__': 'activate_root',
                        'root': root})

Then create an ``autodeps.json`` in your root folder like:

.. code-block:: javascript

    {
      "requirements": ["{root}/requirements.txt"],
      "venv-latest": "{root}/.venv_latest"
    }

Finally, populate ``requirements.txt`` with your python dependencies.

Configuration Options
=====================

-  ``requirements``: A list of paths to ``requirements.txt`` files
   containing deps to install.
-  ``venv-dir-search``: A list of paths to search and create store
   virtualenvs in.
-  ``venv-dir-required-gigabytes``: How much free space is required for
   a path in ``venv-dir-search`` to be used.
-  ``archived-venv-dir``: Path to a directory where tar files of
   virtualenvs are stored and shared between users. You may want to put
   this on a shared filesystem.
-  ``venv-latest``: Path where a symlink to the latest virtual
   environment should be placed.
-  ``submodule-update``: Optional path to a directory to run
   ``git submodule update`` in.
-  ``virtualenv``: Path to virtualenv binary to use. Defaults to bundled
   virtualenv-bootstrap.

