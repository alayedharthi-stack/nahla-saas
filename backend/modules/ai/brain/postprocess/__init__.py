"""Brain postprocess hooks — final-stage checks that run AFTER the
composer has produced ``reply`` but BEFORE the pipeline returns.

Modules in this package observe (and may amend) the reply without
adding new intents, templates, or composer paths. Each hook should:

* Be exception-safe — never raise into the pipeline.
* Be cheap — these run on every turn.
* Emit a single structured log line so the merchant's observability
  pipeline can track misfires across releases.
"""
