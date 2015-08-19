#!/usr/bin/env python
# coding: utf-8
"""
Automatically install dependencies using virtualenv when requirements.txt
changes.
"""

import argparse
import datetime
import errno
import fcntl
import getpass
import hashlib
import json
import os
import platform
import random
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

CONFIG_PATHS = ['autodeps.json']


class Autodeps(object):
  def __init__(self, config_file=None, **kwargs):
    self.config = self.load_config(config_file, kwargs)
    self.lock_file = None
    self.step_prefix = ''  # progress printout only
    self.current_step = 0  # progress printout only
    self.total_steps = 1  # progress printout only
    self.autodeps_dir = os.path.abspath(os.path.dirname(__file__))
    kwargs['autodeps_dir'] = self.autodeps_dir
    kwargs['user'] = getpass.getuser()

    requirements_filenames = [self.expand_path(path, kwargs)
                              for path in self.config['requirements']]
    self.requirements = []
    for path in requirements_filenames:
      self.requirements += [re.sub(r'[#].*', '', line).strip()
                            for line in open(path)]
    self.requirements = filter(len, self.requirements)
    self.deps_hash = hashlib.sha224(platform.python_version() +
                                    repr(self.requirements)).hexdigest()[:40]
    kwargs['deps_hash'] = self.deps_hash

    # Perform a search for a suitable self.virtualenv_dir
    self.virtualenv_dir = None
    paths = [path.format(**kwargs) for path in self.config['venv-dir-search']]
    for path in paths:
      if os.path.exists(path) or os.path.exists(path + '.lock'):
        # Use existing version
        self.virtualenv_dir = path
        break
    if self.virtualenv_dir is None:
      for path in paths:
        if freespace_gb(path) > self.config.get(
                'venv-dir-required-gigabytes', 1.0):
          try:
            os.makedirs(path)
            self.virtualenv_dir = path
            break
          except OSError:
            if os.path.exists(path) or os.path.exists(path + '.lock'):
              # Caught race condition where someone else created it first
              self.virtualenv_dir = path
              break
              # Not enough permissions, try next one
    assert self.virtualenv_dir is not None
    kwargs['venv_dir'] = self.virtualenv_dir

    self.virtualenv_command = self.config['virtualenv'].format(**kwargs)

    # Other paths
    self.activate_this = self.config.get(
        'activate-this-path',
        '{venv_dir}/bin/activate_this.py').format(**kwargs)
    self.lockfilename = self.virtualenv_dir + '.lock'
    self.archive_dir = self.config.get(
        'archived-venv-dir', '').format(**kwargs)
    self.venv_latest_filename = self.expand_path(self.config['venv-latest'],
                                                 kwargs)
    self.submodule_update_path = self.expand_path(
        self.config.get('submodule-update'), kwargs)
    self.pip_extra = self.config.get('pip-args', '').format(**kwargs)

  def load_config(self, config_file, kwargs):
    """
    Load configuration from disk
    """
    config = dict()
    for path in CONFIG_PATHS:
      try:
        config.update(json.load(open(self.expand_path(path, kwargs))))
      except IOError:
        pass  # No configuration
    if config_file:
      config.update(json.load(open(self.expand_path(config_file, kwargs))))
    return config

  @staticmethod
  def expand_path(path, kwargs={}):
    if not path:
      return path
    return os.path.abspath(os.path.join(os.path.dirname(__file__),
                                        path.format(**kwargs)))

  def main(self, args):
    """
    Entry point on the command line
    """
    if args.directory:
      print self.virtualenv_dir
    elif args.archive:
      print os.path.join(self.archive_dir, '{0}.tar.gz'.format(self.deps_hash))
    elif args.install_globally:
      self.pip_install_packages_globally()
    else:
      self.update_if_needed()

  def activate(self):
    """
    Entry point to update sys.path
    """
    self.update_if_needed()
    # Save the path and restore it since the execfile clobbers it. This is a
    # problem since virtualenv will prevent accessing system modules.
    saved_path = os.environ['PATH']
    execfile(self.activate_this, {'__file__': self.activate_this})
    os.environ['PATH'] = saved_path
    os.environ['VENV_DIR'] = self.virtualenv_dir

  def venv_lastest_symlink(self):
    """Create or fix .venv_latest symlink"""
    link_filename = self.venv_latest_filename
    try:
      if not os.path.exists(link_filename) or not os.path.samefile(
              link_filename, self.virtualenv_dir):
        try:
          os.unlink(link_filename)
        except OSError:
          pass  # file does not exist
        os.symlink(self.virtualenv_dir, link_filename)
    except OSError:
      pass  # a race with another autodeps.py process -- ignore it

  def update_if_needed(self):
    """
    Create and populate venv_dir if it does not exist
    """
    self.venv_lastest_symlink()
    if os.path.exists(os.path.join(self.virtualenv_dir, '.completed')):
      return
    old_dir = os.getcwd()
    os.chdir(self.autodeps_dir)
    self.lock_venv_dir()
    if not os.path.exists(os.path.join(self.virtualenv_dir, '.completed')):
      if not self.search_for_precompiled_archive():
        self.info('deps changed, rebuilding virtualenv (this may take a while)')
        self.update_unsafe()
      with open(os.path.join(self.virtualenv_dir, '.completed'), 'w') as fd:
        fd.write(repr(datetime.datetime.now()))
    self.unlock_venv_dir()
    try:
      os.unlink(self.lockfilename)
    except OSError:
      pass
    os.chdir(old_dir)

  def update_unsafe(self):
    """
    Create venv environment without checks or concurrency safety.  Requires
    working directory in script directory.
    """
    self.total_steps = (4 + int(bool(self.submodule_update_path)) +
                        len(self.requirements))

    # Use a extra .build directory then rename it, so that non-relocatable deps
    # errors manifest all the time
    old_venv_dir = self.virtualenv_dir
    self.virtualenv_dir = self.virtualenv_dir + '.build'

    self.mkdirs()
    self.submodule_update()
    self.create_virtualenv()
    self.tag_revision()
    with open(os.path.join(self.virtualenv_dir, 'requirements.txt'), 'w') as fd:
      fd.write('\n'.join(self.requirements) + '\n')
    self.pip_install_packages()
    self.venv_relocatable()
    try:
      shutil.rmtree(old_venv_dir)
    except OSError as error:
      if error.errno != errno.ENOENT:
        raise
    os.rename(self.virtualenv_dir, old_venv_dir)
    self.virtualenv_dir = old_venv_dir

    self.make_archive()

  def search_for_precompiled_archive(self):
    """
    Search for precomputed tar.gz we can use, if found extract it and
    return true
    """
    for search_dir in [self.archive_dir] + list(self.config['venv-dir-search']):
      tarfile = os.path.join(search_dir,
                             '{0}.tar.gz'.format(self.deps_hash))
      if os.path.exists(tarfile):
        try:
          os.makedirs(os.path.dirname(self.virtualenv_dir))
        except OSError:
          pass
        self.total_steps = 2
        self.call_installer('Extracting {0} to {1}'.format(tarfile,
                                                           self.virtualenv_dir),
                            'tar -C "{1}" -xzf "{0}"'.format(
                                tarfile, os.path.dirname(self.virtualenv_dir)))
        self.submodule_update()
        return True
    return False

  def mkdirs(self):
    """
    Create all needed directories for build
    """
    if not os.path.exists(self.virtualenv_dir):
      try:
        os.makedirs(self.virtualenv_dir)
      except:
        pass

  def lock_venv_dir(self):
    """
    Multiple processes might want the same environment, use posix file
    locking to make only one of them builds it
    """
    self.lock_file = open(self.lockfilename, 'a')
    fcntl.lockf(self.lock_file, fcntl.LOCK_EX)

  def unlock_venv_dir(self):
    """
    The opposite of lock_venv_dir()
    """
    fcntl.lockf(self.lock_file, fcntl.LOCK_UN)
    self.lock_file.close()
    self.lock_file = None

  def submodule_update(self):
    if self.submodule_update_path:
      try:
        self.call_installer('Updating submodules',
                            'cd {} && git submodule update --init'.format(
                                self.submodule_update_path),
                            os.path.join(self.virtualenv_dir, 'submodules.log'))
      except:
        pass  # error will already be printed to stderr

  def create_virtualenv(self):
    self.call_installer('Creating venv {0}'.format(self.virtualenv_dir),
                        '{} --always-copy "{}"'.format(self.virtualenv_command,
                                                       self.virtualenv_dir),
                        os.path.join(self.virtualenv_dir, 'venv.log'))

  def tag_revision(self):
    self.call_installer('Recording git revision',
                        'git show HEAD | head -n 3',
                        os.path.join(self.virtualenv_dir, 'revision'))

  def venv_relocatable(self):
    self.call_installer('Making venv relocatable',
                        '"{}" --relocatable --system-site-packages "{}"'
                        .format(self.virtualenv_command,
                                self.virtualenv_dir),
                        os.path.join(self.virtualenv_dir, 'relocatable.log'))

  def make_archive(self):
    """
    Try to tar up venv_dir in a public place so others can use it
    """
    if not os.access(self.archive_dir, os.W_OK):
      return
    tmpdst = os.path.join(self.archive_dir, '{0}_{1}.tar.gz'.format(
        self.deps_hash, random.randint(0, 2 ** 31)))
    dst = os.path.join(self.archive_dir,
                       '{0}.tar.gz'.format(self.deps_hash))
    if os.path.exists(dst):
      return
    srcdir, srcfile = os.path.split(self.virtualenv_dir)
    self.call_installer('Making tar archive',
                        'tar -C "{1}" -czf "{0}" {2}'.format(tmpdst, srcdir,
                                                             srcfile),
                        os.path.join(self.virtualenv_dir, 'archive.log'))
    os.chmod(tmpdst, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
    os.rename(tmpdst, dst)

  def pip_install_packages(self, globally=False):
    """
    Install all of the required pip packages to the virtual environment
    """
    self.step_prefix = 'Installing pip package '
    for package in self.requirements:
      match = re.match('.*/([a-zA-Z-]+)', package)
      if match is None:
        match = re.search(' ([a-zA-Z]+)$', package)
      package_short = match.group(1) if match else package
      logfilename = os.path.join(self.virtualenv_dir, package_short + '.out')
      logfilename_verbose = os.path.join(self.virtualenv_dir,
                                         package_short + '.log')
      if globally:
        cmd = 'python pip install {extra} --upgrade {0}'.format(
            package, extra=self.pip_extra)
      else:
        python = os.path.join(self.virtualenv_dir, 'bin/python')
        cmd = ('"{python}" "{self.virtualenv_dir}/bin/pip" --log="{logv}"'
               ' install {self.pip_extra} {0}'.format(package,
                                                      python=python,
                                                      self=self,
                                                      logv=logfilename_verbose))
      self.call_installer(package_short, cmd, logfilename)
    self.step_prefix = ''

  def pip_install_packages_globally(self):
    assert getpass.getuser() == 'root'
    self.total_steps = len(self.requirements)
    self.virtualenv_dir = tempfile.mkdtemp('logs')
    self.pip_install_packages(globally=True)
    shutil.rmtree(self.virtualenv_dir)

  def info(self, msg):
    sys.stderr.write('[autodeps] {0}\n'.format(msg))

  def call_installer(self, name, cmd, logfilename=None):
    """
    Run a given install command and print progress to stderr
    """
    self.current_step += 1
    sys.stderr.write(
        '[{self.current_step:>2}/{self.total_steps:>2}] {self.step_prefix}{0}..'
        .format(name, self=self))
    if logfilename:
      logfile = open(logfilename, 'w')
    else:
      logfile = sys.stderr
    process = subprocess.Popen(cmd, shell=True, stdout=logfile, stderr=logfile)
    if logfilename:
      old_size = os.path.getsize(logfilename)
    else:
      old_size = 0
    n_iterations = 0
    start = time.time()
    while process.returncode is None:
      n_iterations += 1
      time.sleep(0.05)
      if logfilename:
        size = os.path.getsize(logfilename)
      else:
        size = 0
      if size != old_size:
        old_size = size
        sys.stderr.write('.')
      if (n_iterations % 100) == 0:
        sys.stderr.write(' ')
      process.poll()
    if process.returncode == 0:
      sys.stderr.write(' OK ({0:.2f} sec)\n'.format(time.time() - start))
    else:
      sys.stderr.write(' ERROR (see {0})\n'.format(logfilename))
      raise Exception('Failed to update dependencies (see {0})\n'.format(
          logfilename))


def freespace_gb(path):
  while not os.path.exists(path) and path:
    path = os.path.dirname(path)
  s = os.statvfs(path)
  return s.f_bsize * s.f_bavail / 1024.0 ** 3


def main():
  parser = argparse.ArgumentParser()
  parser.add_argument('--root', help='Root path to activate')
  parser.add_argument('--directory', action='store_true',
                      help='Print path of venv directory and exit')
  parser.add_argument('--archive', action='store_true',
                      help='Print path of archive tarball and exit')
  parser.add_argument('--install-globally', action='store_true',
                      help='Install dependencies system wide')
  args = parser.parse_args()
  if args.root:
    with_root_path(args.root).main(args)
  else:
    Autodeps().main(args)


def with_root_path(root):
  root = os.path.abspath(root)
  config_file = os.path.join(root, 'autodeps.json')
  return Autodeps(config_file=config_file, root=root)


if __name__ == '__main__':
  main()
elif __name__ == 'activate':
  Autodeps(**vars().get('kwargs', {})).activate()
elif __name__ == 'activate_root':
  # execfile() caller is expected to provide 'root' var
  with_root_path(vars()['root']).activate()
