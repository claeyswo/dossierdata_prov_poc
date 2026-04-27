"""Engine-provided plugin-agnostic components.

Modules under here implement features that are mechanism, not policy:
the plugin YAML opts in with a configuration block, the engine does
the rest. Currently:

* ``exceptions`` — the exception-grant lifecycle (grant / retract /
  consume activities, the ``valideer_exception`` validator, the two
  handlers). Opt-in block: ``exceptions:`` at the workflow top level.

Kept as a subpackage so each mechanism stays self-contained — the
activity defs live next to their validator and handlers, not scattered
across engine-internals files.
"""
