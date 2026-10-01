"""watch_core for the job-scout scripts of the agent plugin: no copy, the package of the agent plugin itself.

The job-scout scripts run from this folder (behaviors/job-scout/scripts) and import `watch_core` as a sibling, as they
did in the old job-scout plugin. This package points its search path at skills/agent/scripts/watch_core and runs that
package's __init__, so `from watch_core import planou` loads the very module agent.py and runner.sh load.
"""
import os

__path__ = [os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', '..', 'scripts', 'watch_core'))]
_init = os.path.join(__path__[0], '__init__.py')
with open(_init, encoding='utf-8') as _f:
    exec(compile(_f.read(), _init, 'exec'))
del _f, _init
