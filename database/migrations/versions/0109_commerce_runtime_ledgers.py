"""Commerce runtime ledgers — dormant persistence for reserved external
mutations (effects) and outbound delivery sequences, with append-only
attempts, results and receipts.

Dormant by construction
=======================
The six tables belong to ``core.commerce_runtime.models.RuntimeBase`` (the
foundation's metadata, declared in ``core.commerce_runtime.ledger_models``),
deliberately separate from the application ``models.Base``. Production
startup pins ``alembic upgrade 0093`` and materialises only ``models.Base``
through ``create_all`` (``backend/main.py``), so this revision changes no
production database unless an owner applies it explicitly. Nothing reads or
writes these tables at runtime; the only callers are the ledger tests.

Graph
=====
The repository intentionally carries two heads: ``0092`` (A1-Validate branch)
and the integration-bootstrap chain that this revision extends (``0108`` →
``0109``). Do not use ``alembic upgrade head``. Apply with
``alembic upgrade 0109`` on a database at ``0108``; ``alembic downgrade 0108``
removes everything this revision created.

Reconciliation policy (explicit; nothing is stamped silently)
=============================================================
Exactly these pre-existing states are reconciled:

* a ledger table is absent → it is created as defined here;
* a ledger index is absent → created;
* the append-only trigger function is absent → created;
* the append-only trigger is absent **on one of the four append-only ledger
  relations** → created there (a same-named trigger on any other relation
  is irrelevant).

Every other pre-existing shape is verified **by definition** against the
schema this revision produces on a fresh database (columns, constraints,
indexes, each trigger's relation, enabled state, timing, events, row level
and function, and the function body). Any difference raises, the
transaction aborts and ``alembic_version`` is not advanced. The verifier
(``schema_differences``) is also the post-condition of a successful upgrade.

Revision ID: 0109
Revises: 0108
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from migration_inspector_helpers import has_index, has_table


revision = "0109"
down_revision = "0108"
branch_labels = None
depends_on = None

_CONVERSATIONS = "commerce_runtime_conversations"
_TURNS = "commerce_runtime_turns"
_EFFECTS = "commerce_runtime_effects"
_EFFECT_ATTEMPTS = "commerce_runtime_effect_attempts"
_EFFECT_RESULTS = "commerce_runtime_effect_results"
_DELIVERY_SEQUENCES = "commerce_runtime_delivery_sequences"
_DELIVERY_ATTEMPTS = "commerce_runtime_delivery_attempts"
_DELIVERY_RECEIPTS = "commerce_runtime_delivery_receipts"
_APPEND_ONLY = (_EFFECT_ATTEMPTS, _EFFECT_RESULTS, _DELIVERY_ATTEMPTS, _DELIVERY_RECEIPTS)
_INDEXES = (
    (_EFFECTS, "ix_commerce_runtime_effects_turn", ["turn_id"]),
    (_EFFECT_RESULTS, "ix_commerce_runtime_effect_results_effect", ["effect_id"]),
    (_DELIVERY_RECEIPTS, "ix_commerce_runtime_delivery_receipts_sequence", ["sequence_id"]),
)
_TRIGGER_FUNCTION = "commerce_runtime_ledger_rows_immutable"
_TRIGGER = "trg_commerce_runtime_ledger_immutable"

_NAMESPACE_SQL = "namespace IN ('live', 'shadow')"

# Definitions of the schema this revision produces on a fresh database, as
# PostgreSQL reports them. Generated from a fresh ``alembic upgrade 0109``
# and used to verify any pre-existing shape by definition.
_EXPECTED = {'commerce_runtime_effects': {'columns': (('id',
                                           'int8',
                                           None,
                                           'NO',
                                           "nextval('commerce_runtime_effects_id_seq'::regclass)"),
                                          ('tenant_id', 'int4', None, 'NO', None),
                                          ('namespace', 'varchar', 16, 'NO', None),
                                          ('conversation_id', 'int8', None, 'NO', None),
                                          ('turn_id', 'int8', None, 'NO', None),
                                          ('action_type', 'varchar', 64, 'NO', None),
                                          ('idempotency_key', 'varchar', 128, 'NO', None),
                                          ('payload', 'jsonb', None, 'NO', "'{}'::jsonb"),
                                          ('payload_hash', 'varchar', 64, 'NO', None),
                                          ('status', 'varchar', 32, 'NO', "'reserved'::character varying"),
                                          ('attempt_count', 'int8', None, 'NO', '0'),
                                          ('reserved_by', 'varchar', 128, 'NO', None),
                                          ('reserved_fence', 'int8', None, 'NO', None),
                                          ('reserved_epoch', 'int8', None, 'NO', None),
                                          ('confirmed_result', 'jsonb', None, 'YES', None),
                                          ('created_at', 'timestamptz', None, 'NO', 'now()'),
                                          ('updated_at', 'timestamptz', None, 'NO', 'now()')),
                              'constraints': {'ck_commerce_runtime_effects_attempt_count': ('c',
                                                                                            'CHECK '
                                                                                            '((attempt_count '
                                                                                            '>= 0))'),
                                              'ck_commerce_runtime_effects_confirmed_pair': ('c',
                                                                                             'CHECK '
                                                                                             '((((status)::text '
                                                                                             '= '
                                                                                             "'confirmed'::text) "
                                                                                             '= '
                                                                                             '(confirmed_result '
                                                                                             'IS NOT '
                                                                                             'NULL)))'),
                                              'ck_commerce_runtime_effects_namespace': ('c',
                                                                                        'CHECK '
                                                                                        '(((namespace)::text '
                                                                                        '= ANY '
                                                                                        "((ARRAY['live'::character "
                                                                                        'varying, '
                                                                                        "'shadow'::character "
                                                                                        'varying])::text[])))'),
                                              'ck_commerce_runtime_effects_status': ('c',
                                                                                     'CHECK (((status)::text '
                                                                                     '= ANY '
                                                                                     "((ARRAY['reserved'::character "
                                                                                     'varying, '
                                                                                     "'dispatching'::character "
                                                                                     'varying, '
                                                                                     "'confirmed'::character "
                                                                                     'varying, '
                                                                                     "'rejected'::character "
                                                                                     'varying, '
                                                                                     "'unknown'::character "
                                                                                     'varying])::text[])))'),
                                              'commerce_runtime_effects_pkey': ('p', 'PRIMARY KEY (id)'),
                                              'fk_commerce_runtime_effects_conversation_scope': ('f',
                                                                                                 'FOREIGN '
                                                                                                 'KEY '
                                                                                                 '(conversation_id, '
                                                                                                 'tenant_id, '
                                                                                                 'namespace) '
                                                                                                 'REFERENCES '
                                                                                                 'commerce_runtime_conversations(id, '
                                                                                                 'tenant_id, '
                                                                                                 'namespace)'),
                                              'fk_commerce_runtime_effects_tenant': ('f',
                                                                                     'FOREIGN KEY '
                                                                                     '(tenant_id) REFERENCES '
                                                                                     'tenants(id)'),
                                              'fk_commerce_runtime_effects_turn_scope': ('f',
                                                                                         'FOREIGN KEY '
                                                                                         '(turn_id, '
                                                                                         'tenant_id, '
                                                                                         'namespace) '
                                                                                         'REFERENCES '
                                                                                         'commerce_runtime_turns(id, '
                                                                                         'tenant_id, '
                                                                                         'namespace)'),
                                              'uq_commerce_runtime_effects_key': ('u',
                                                                                  'UNIQUE (tenant_id, '
                                                                                  'namespace, '
                                                                                  'idempotency_key)'),
                                              'uq_commerce_runtime_effects_scope': ('u',
                                                                                    'UNIQUE (id, tenant_id, '
                                                                                    'namespace, '
                                                                                    'conversation_id)')},
                              'indexes': {'commerce_runtime_effects_pkey': 'CREATE UNIQUE INDEX '
                                                                           'commerce_runtime_effects_pkey ON '
                                                                           'public.commerce_runtime_effects '
                                                                           'USING btree (id)',
                                          'ix_commerce_runtime_effects_turn': 'CREATE INDEX '
                                                                              'ix_commerce_runtime_effects_turn '
                                                                              'ON '
                                                                              'public.commerce_runtime_effects '
                                                                              'USING btree (turn_id)',
                                          'uq_commerce_runtime_effects_key': 'CREATE UNIQUE INDEX '
                                                                             'uq_commerce_runtime_effects_key '
                                                                             'ON '
                                                                             'public.commerce_runtime_effects '
                                                                             'USING btree (tenant_id, '
                                                                             'namespace, idempotency_key)',
                                          'uq_commerce_runtime_effects_scope': 'CREATE UNIQUE INDEX '
                                                                               'uq_commerce_runtime_effects_scope '
                                                                               'ON '
                                                                               'public.commerce_runtime_effects '
                                                                               'USING btree (id, tenant_id, '
                                                                               'namespace, '
                                                                               'conversation_id)'}},
 'commerce_runtime_effect_attempts': {'columns': (('id',
                                                   'int8',
                                                   None,
                                                   'NO',
                                                   "nextval('commerce_runtime_effect_attempts_id_seq'::regclass)"),
                                                  ('effect_id', 'int8', None, 'NO', None),
                                                  ('tenant_id', 'int4', None, 'NO', None),
                                                  ('namespace', 'varchar', 16, 'NO', None),
                                                  ('conversation_id', 'int8', None, 'NO', None),
                                                  ('attempt_no', 'int8', None, 'NO', None),
                                                  ('dispatch_key', 'varchar', 64, 'NO', None),
                                                  ('reserved_by', 'varchar', 128, 'NO', None),
                                                  ('reserved_fence', 'int8', None, 'NO', None),
                                                  ('reserved_epoch', 'int8', None, 'NO', None),
                                                  ('reserved_at', 'timestamptz', None, 'NO', 'now()')),
                                      'constraints': {'ck_commerce_runtime_effect_attempts_attempt_no': ('c',
                                                                                                         'CHECK '
                                                                                                         '((attempt_no '
                                                                                                         '>= '
                                                                                                         '1))'),
                                                      'ck_commerce_runtime_effect_attempts_namespace': ('c',
                                                                                                        'CHECK '
                                                                                                        '(((namespace)::text '
                                                                                                        '= '
                                                                                                        'ANY '
                                                                                                        "((ARRAY['live'::character "
                                                                                                        'varying, '
                                                                                                        "'shadow'::character "
                                                                                                        'varying])::text[])))'),
                                                      'commerce_runtime_effect_attempts_pkey': ('p',
                                                                                                'PRIMARY KEY '
                                                                                                '(id)'),
                                                      'fk_commerce_runtime_effect_attempts_effect_scope': ('f',
                                                                                                           'FOREIGN '
                                                                                                           'KEY '
                                                                                                           '(effect_id, '
                                                                                                           'tenant_id, '
                                                                                                           'namespace, '
                                                                                                           'conversation_id) '
                                                                                                           'REFERENCES '
                                                                                                           'commerce_runtime_effects(id, '
                                                                                                           'tenant_id, '
                                                                                                           'namespace, '
                                                                                                           'conversation_id)'),
                                                      'uq_commerce_runtime_effect_attempts_dispatch_key': ('u',
                                                                                                           'UNIQUE '
                                                                                                           '(dispatch_key)'),
                                                      'uq_commerce_runtime_effect_attempts_order': ('u',
                                                                                                    'UNIQUE '
                                                                                                    '(effect_id, '
                                                                                                    'attempt_no)'),
                                                      'uq_commerce_runtime_effect_attempts_scope': ('u',
                                                                                                    'UNIQUE '
                                                                                                    '(id, '
                                                                                                    'effect_id)')},
                                      'indexes': {'commerce_runtime_effect_attempts_pkey': 'CREATE UNIQUE '
                                                                                           'INDEX '
                                                                                           'commerce_runtime_effect_attempts_pkey '
                                                                                           'ON '
                                                                                           'public.commerce_runtime_effect_attempts '
                                                                                           'USING btree (id)',
                                                  'uq_commerce_runtime_effect_attempts_dispatch_key': 'CREATE '
                                                                                                      'UNIQUE '
                                                                                                      'INDEX '
                                                                                                      'uq_commerce_runtime_effect_attempts_dispatch_key '
                                                                                                      'ON '
                                                                                                      'public.commerce_runtime_effect_attempts '
                                                                                                      'USING '
                                                                                                      'btree '
                                                                                                      '(dispatch_key)',
                                                  'uq_commerce_runtime_effect_attempts_order': 'CREATE '
                                                                                               'UNIQUE INDEX '
                                                                                               'uq_commerce_runtime_effect_attempts_order '
                                                                                               'ON '
                                                                                               'public.commerce_runtime_effect_attempts '
                                                                                               'USING btree '
                                                                                               '(effect_id, '
                                                                                               'attempt_no)',
                                                  'uq_commerce_runtime_effect_attempts_scope': 'CREATE '
                                                                                               'UNIQUE INDEX '
                                                                                               'uq_commerce_runtime_effect_attempts_scope '
                                                                                               'ON '
                                                                                               'public.commerce_runtime_effect_attempts '
                                                                                               'USING btree '
                                                                                               '(id, '
                                                                                               'effect_id)'}},
 'commerce_runtime_effect_results': {'columns': (('id',
                                                  'int8',
                                                  None,
                                                  'NO',
                                                  "nextval('commerce_runtime_effect_results_id_seq'::regclass)"),
                                                 ('attempt_id', 'int8', None, 'NO', None),
                                                 ('effect_id', 'int8', None, 'NO', None),
                                                 ('result_no', 'int8', None, 'NO', None),
                                                 ('outcome', 'varchar', 32, 'NO', None),
                                                 ('evidence', 'jsonb', None, 'NO', "'{}'::jsonb"),
                                                 ('recorded_by', 'varchar', 128, 'NO', None),
                                                 ('recorded_at', 'timestamptz', None, 'NO', 'now()')),
                                     'constraints': {'ck_commerce_runtime_effect_results_outcome': ('c',
                                                                                                    'CHECK '
                                                                                                    '(((outcome)::text '
                                                                                                    '= ANY '
                                                                                                    "((ARRAY['confirmed'::character "
                                                                                                    'varying, '
                                                                                                    "'rejected'::character "
                                                                                                    'varying, '
                                                                                                    "'unknown'::character "
                                                                                                    'varying])::text[])))'),
                                                     'ck_commerce_runtime_effect_results_result_no': ('c',
                                                                                                      'CHECK '
                                                                                                      '((result_no '
                                                                                                      '>= '
                                                                                                      '1))'),
                                                     'commerce_runtime_effect_results_pkey': ('p',
                                                                                              'PRIMARY KEY '
                                                                                              '(id)'),
                                                     'fk_commerce_runtime_effect_results_attempt_scope': ('f',
                                                                                                          'FOREIGN '
                                                                                                          'KEY '
                                                                                                          '(attempt_id, '
                                                                                                          'effect_id) '
                                                                                                          'REFERENCES '
                                                                                                          'commerce_runtime_effect_attempts(id, '
                                                                                                          'effect_id)'),
                                                     'uq_commerce_runtime_effect_results_order': ('u',
                                                                                                  'UNIQUE '
                                                                                                  '(attempt_id, '
                                                                                                  'result_no)')},
                                     'indexes': {'commerce_runtime_effect_results_pkey': 'CREATE UNIQUE '
                                                                                         'INDEX '
                                                                                         'commerce_runtime_effect_results_pkey '
                                                                                         'ON '
                                                                                         'public.commerce_runtime_effect_results '
                                                                                         'USING btree (id)',
                                                 'ix_commerce_runtime_effect_results_effect': 'CREATE INDEX '
                                                                                              'ix_commerce_runtime_effect_results_effect '
                                                                                              'ON '
                                                                                              'public.commerce_runtime_effect_results '
                                                                                              'USING btree '
                                                                                              '(effect_id)',
                                                 'uq_commerce_runtime_effect_results_order': 'CREATE UNIQUE '
                                                                                             'INDEX '
                                                                                             'uq_commerce_runtime_effect_results_order '
                                                                                             'ON '
                                                                                             'public.commerce_runtime_effect_results '
                                                                                             'USING btree '
                                                                                             '(attempt_id, '
                                                                                             'result_no)'}},
 'commerce_runtime_delivery_sequences': {'columns': (('id',
                                                      'int8',
                                                      None,
                                                      'NO',
                                                      "nextval('commerce_runtime_delivery_sequences_id_seq'::regclass)"),
                                                     ('tenant_id', 'int4', None, 'NO', None),
                                                     ('namespace', 'varchar', 16, 'NO', None),
                                                     ('conversation_id', 'int8', None, 'NO', None),
                                                     ('turn_id', 'int8', None, 'NO', None),
                                                     ('intent_kind', 'varchar', 16, 'NO', None),
                                                     ('intent_payload', 'jsonb', None, 'NO', "'{}'::jsonb"),
                                                     ('intent_hash', 'varchar', 64, 'NO', None),
                                                     ('attempt_count', 'int8', None, 'NO', '0'),
                                                     ('outcome',
                                                      'varchar',
                                                      32,
                                                      'NO',
                                                      "'pending'::character varying"),
                                                     ('reserved_by', 'varchar', 128, 'NO', None),
                                                     ('reserved_fence', 'int8', None, 'NO', None),
                                                     ('reserved_epoch', 'int8', None, 'NO', None),
                                                     ('created_at', 'timestamptz', None, 'NO', 'now()'),
                                                     ('updated_at', 'timestamptz', None, 'NO', 'now()')),
                                         'constraints': {'ck_commerce_runtime_delivery_sequences_attempt_count': ('c',
                                                                                                                  'CHECK '
                                                                                                                  '((attempt_count '
                                                                                                                  '>= '
                                                                                                                  '0))'),
                                                         'ck_commerce_runtime_delivery_sequences_intent_kind': ('c',
                                                                                                                'CHECK '
                                                                                                                '(((intent_kind)::text '
                                                                                                                '= '
                                                                                                                'ANY '
                                                                                                                "((ARRAY['rich'::character "
                                                                                                                'varying, '
                                                                                                                "'text'::character "
                                                                                                                'varying])::text[])))'),
                                                         'ck_commerce_runtime_delivery_sequences_namespace': ('c',
                                                                                                              'CHECK '
                                                                                                              '(((namespace)::text '
                                                                                                              '= '
                                                                                                              'ANY '
                                                                                                              "((ARRAY['live'::character "
                                                                                                              'varying, '
                                                                                                              "'shadow'::character "
                                                                                                              'varying])::text[])))'),
                                                         'ck_commerce_runtime_delivery_sequences_outcome': ('c',
                                                                                                            'CHECK '
                                                                                                            '(((outcome)::text '
                                                                                                            '= '
                                                                                                            'ANY '
                                                                                                            "((ARRAY['pending'::character "
                                                                                                            'varying, '
                                                                                                            "'accepted'::character "
                                                                                                            'varying, '
                                                                                                            "'rejected'::character "
                                                                                                            'varying, '
                                                                                                            "'unknown'::character "
                                                                                                            'varying])::text[])))'),
                                                         'commerce_runtime_delivery_sequences_pkey': ('p',
                                                                                                      'PRIMARY '
                                                                                                      'KEY '
                                                                                                      '(id)'),
                                                         'fk_commerce_runtime_delivery_sequences_conversation_scope': ('f',
                                                                                                                       'FOREIGN '
                                                                                                                       'KEY '
                                                                                                                       '(conversation_id, '
                                                                                                                       'tenant_id, '
                                                                                                                       'namespace) '
                                                                                                                       'REFERENCES '
                                                                                                                       'commerce_runtime_conversations(id, '
                                                                                                                       'tenant_id, '
                                                                                                                       'namespace)'),
                                                         'fk_commerce_runtime_delivery_sequences_tenant': ('f',
                                                                                                           'FOREIGN '
                                                                                                           'KEY '
                                                                                                           '(tenant_id) '
                                                                                                           'REFERENCES '
                                                                                                           'tenants(id)'),
                                                         'fk_commerce_runtime_delivery_sequences_turn_scope': ('f',
                                                                                                               'FOREIGN '
                                                                                                               'KEY '
                                                                                                               '(turn_id, '
                                                                                                               'tenant_id, '
                                                                                                               'namespace) '
                                                                                                               'REFERENCES '
                                                                                                               'commerce_runtime_turns(id, '
                                                                                                               'tenant_id, '
                                                                                                               'namespace)'),
                                                         'uq_commerce_runtime_delivery_sequences_scope': ('u',
                                                                                                          'UNIQUE '
                                                                                                          '(id, '
                                                                                                          'tenant_id, '
                                                                                                          'namespace, '
                                                                                                          'conversation_id)'),
                                                         'uq_commerce_runtime_delivery_sequences_turn': ('u',
                                                                                                         'UNIQUE '
                                                                                                         '(turn_id)')},
                                         'indexes': {'commerce_runtime_delivery_sequences_pkey': 'CREATE '
                                                                                                 'UNIQUE '
                                                                                                 'INDEX '
                                                                                                 'commerce_runtime_delivery_sequences_pkey '
                                                                                                 'ON '
                                                                                                 'public.commerce_runtime_delivery_sequences '
                                                                                                 'USING '
                                                                                                 'btree (id)',
                                                     'uq_commerce_runtime_delivery_sequences_scope': 'CREATE '
                                                                                                     'UNIQUE '
                                                                                                     'INDEX '
                                                                                                     'uq_commerce_runtime_delivery_sequences_scope '
                                                                                                     'ON '
                                                                                                     'public.commerce_runtime_delivery_sequences '
                                                                                                     'USING '
                                                                                                     'btree '
                                                                                                     '(id, '
                                                                                                     'tenant_id, '
                                                                                                     'namespace, '
                                                                                                     'conversation_id)',
                                                     'uq_commerce_runtime_delivery_sequences_turn': 'CREATE '
                                                                                                    'UNIQUE '
                                                                                                    'INDEX '
                                                                                                    'uq_commerce_runtime_delivery_sequences_turn '
                                                                                                    'ON '
                                                                                                    'public.commerce_runtime_delivery_sequences '
                                                                                                    'USING '
                                                                                                    'btree '
                                                                                                    '(turn_id)'}},
 'commerce_runtime_delivery_attempts': {'columns': (('id',
                                                     'int8',
                                                     None,
                                                     'NO',
                                                     "nextval('commerce_runtime_delivery_attempts_id_seq'::regclass)"),
                                                    ('sequence_id', 'int8', None, 'NO', None),
                                                    ('tenant_id', 'int4', None, 'NO', None),
                                                    ('namespace', 'varchar', 16, 'NO', None),
                                                    ('conversation_id', 'int8', None, 'NO', None),
                                                    ('attempt_no', 'int8', None, 'NO', None),
                                                    ('kind', 'varchar', 16, 'NO', None),
                                                    ('dispatch_key', 'varchar', 64, 'NO', None),
                                                    ('payload', 'jsonb', None, 'NO', "'{}'::jsonb"),
                                                    ('reserved_by', 'varchar', 128, 'NO', None),
                                                    ('reserved_fence', 'int8', None, 'NO', None),
                                                    ('reserved_epoch', 'int8', None, 'NO', None),
                                                    ('reserved_at', 'timestamptz', None, 'NO', 'now()')),
                                        'constraints': {'ck_commerce_runtime_delivery_attempts_attempt_no': ('c',
                                                                                                             'CHECK '
                                                                                                             '((attempt_no '
                                                                                                             '>= '
                                                                                                             '1))'),
                                                        'ck_commerce_runtime_delivery_attempts_kind': ('c',
                                                                                                       'CHECK '
                                                                                                       '(((kind)::text '
                                                                                                       '= '
                                                                                                       'ANY '
                                                                                                       "((ARRAY['rich'::character "
                                                                                                       'varying, '
                                                                                                       "'text'::character "
                                                                                                       'varying])::text[])))'),
                                                        'ck_commerce_runtime_delivery_attempts_namespace': ('c',
                                                                                                            'CHECK '
                                                                                                            '(((namespace)::text '
                                                                                                            '= '
                                                                                                            'ANY '
                                                                                                            "((ARRAY['live'::character "
                                                                                                            'varying, '
                                                                                                            "'shadow'::character "
                                                                                                            'varying])::text[])))'),
                                                        'commerce_runtime_delivery_attempts_pkey': ('p',
                                                                                                    'PRIMARY '
                                                                                                    'KEY '
                                                                                                    '(id)'),
                                                        'fk_commerce_runtime_delivery_attempts_sequence_scope': ('f',
                                                                                                                 'FOREIGN '
                                                                                                                 'KEY '
                                                                                                                 '(sequence_id, '
                                                                                                                 'tenant_id, '
                                                                                                                 'namespace, '
                                                                                                                 'conversation_id) '
                                                                                                                 'REFERENCES '
                                                                                                                 'commerce_runtime_delivery_sequences(id, '
                                                                                                                 'tenant_id, '
                                                                                                                 'namespace, '
                                                                                                                 'conversation_id)'),
                                                        'uq_commerce_runtime_delivery_attempts_dispatch_key': ('u',
                                                                                                               'UNIQUE '
                                                                                                               '(dispatch_key)'),
                                                        'uq_commerce_runtime_delivery_attempts_order': ('u',
                                                                                                        'UNIQUE '
                                                                                                        '(sequence_id, '
                                                                                                        'attempt_no)'),
                                                        'uq_commerce_runtime_delivery_attempts_scope': ('u',
                                                                                                        'UNIQUE '
                                                                                                        '(id, '
                                                                                                        'sequence_id)')},
                                        'indexes': {'commerce_runtime_delivery_attempts_pkey': 'CREATE '
                                                                                               'UNIQUE INDEX '
                                                                                               'commerce_runtime_delivery_attempts_pkey '
                                                                                               'ON '
                                                                                               'public.commerce_runtime_delivery_attempts '
                                                                                               'USING btree '
                                                                                               '(id)',
                                                    'uq_commerce_runtime_delivery_attempts_dispatch_key': 'CREATE '
                                                                                                          'UNIQUE '
                                                                                                          'INDEX '
                                                                                                          'uq_commerce_runtime_delivery_attempts_dispatch_key '
                                                                                                          'ON '
                                                                                                          'public.commerce_runtime_delivery_attempts '
                                                                                                          'USING '
                                                                                                          'btree '
                                                                                                          '(dispatch_key)',
                                                    'uq_commerce_runtime_delivery_attempts_order': 'CREATE '
                                                                                                   'UNIQUE '
                                                                                                   'INDEX '
                                                                                                   'uq_commerce_runtime_delivery_attempts_order '
                                                                                                   'ON '
                                                                                                   'public.commerce_runtime_delivery_attempts '
                                                                                                   'USING '
                                                                                                   'btree '
                                                                                                   '(sequence_id, '
                                                                                                   'attempt_no)',
                                                    'uq_commerce_runtime_delivery_attempts_scope': 'CREATE '
                                                                                                   'UNIQUE '
                                                                                                   'INDEX '
                                                                                                   'uq_commerce_runtime_delivery_attempts_scope '
                                                                                                   'ON '
                                                                                                   'public.commerce_runtime_delivery_attempts '
                                                                                                   'USING '
                                                                                                   'btree '
                                                                                                   '(id, '
                                                                                                   'sequence_id)'}},
 'commerce_runtime_delivery_receipts': {'columns': (('id',
                                                     'int8',
                                                     None,
                                                     'NO',
                                                     "nextval('commerce_runtime_delivery_receipts_id_seq'::regclass)"),
                                                    ('attempt_id', 'int8', None, 'NO', None),
                                                    ('sequence_id', 'int8', None, 'NO', None),
                                                    ('receipt_no', 'int8', None, 'NO', None),
                                                    ('kind', 'varchar', 32, 'NO', None),
                                                    ('provider_message_id', 'varchar', 256, 'YES', None),
                                                    ('evidence', 'jsonb', None, 'NO', "'{}'::jsonb"),
                                                    ('recorded_by', 'varchar', 128, 'NO', None),
                                                    ('recorded_at', 'timestamptz', None, 'NO', 'now()')),
                                        'constraints': {'ck_commerce_runtime_delivery_receipts_accepted_id': ('c',
                                                                                                              'CHECK '
                                                                                                              '((((kind)::text '
                                                                                                              '<> '
                                                                                                              "'accepted'::text) "
                                                                                                              'OR '
                                                                                                              '(provider_message_id '
                                                                                                              'IS '
                                                                                                              'NOT '
                                                                                                              'NULL)))'),
                                                        'ck_commerce_runtime_delivery_receipts_kind': ('c',
                                                                                                       'CHECK '
                                                                                                       '(((kind)::text '
                                                                                                       '= '
                                                                                                       'ANY '
                                                                                                       "((ARRAY['accepted'::character "
                                                                                                       'varying, '
                                                                                                       "'rejected'::character "
                                                                                                       'varying, '
                                                                                                       "'unknown'::character "
                                                                                                       'varying, '
                                                                                                       "'delivered'::character "
                                                                                                       'varying, '
                                                                                                       "'read'::character "
                                                                                                       'varying, '
                                                                                                       "'failed'::character "
                                                                                                       'varying])::text[])))'),
                                                        'ck_commerce_runtime_delivery_receipts_receipt_no': ('c',
                                                                                                             'CHECK '
                                                                                                             '((receipt_no '
                                                                                                             '>= '
                                                                                                             '1))'),
                                                        'commerce_runtime_delivery_receipts_pkey': ('p',
                                                                                                    'PRIMARY '
                                                                                                    'KEY '
                                                                                                    '(id)'),
                                                        'fk_commerce_runtime_delivery_receipts_attempt_scope': ('f',
                                                                                                                'FOREIGN '
                                                                                                                'KEY '
                                                                                                                '(attempt_id, '
                                                                                                                'sequence_id) '
                                                                                                                'REFERENCES '
                                                                                                                'commerce_runtime_delivery_attempts(id, '
                                                                                                                'sequence_id)'),
                                                        'uq_commerce_runtime_delivery_receipts_order': ('u',
                                                                                                        'UNIQUE '
                                                                                                        '(attempt_id, '
                                                                                                        'receipt_no)')},
                                        'indexes': {'commerce_runtime_delivery_receipts_pkey': 'CREATE '
                                                                                               'UNIQUE INDEX '
                                                                                               'commerce_runtime_delivery_receipts_pkey '
                                                                                               'ON '
                                                                                               'public.commerce_runtime_delivery_receipts '
                                                                                               'USING btree '
                                                                                               '(id)',
                                                    'ix_commerce_runtime_delivery_receipts_sequence': 'CREATE '
                                                                                                      'INDEX '
                                                                                                      'ix_commerce_runtime_delivery_receipts_sequence '
                                                                                                      'ON '
                                                                                                      'public.commerce_runtime_delivery_receipts '
                                                                                                      'USING '
                                                                                                      'btree '
                                                                                                      '(sequence_id)',
                                                    'uq_commerce_runtime_delivery_receipts_order': 'CREATE '
                                                                                                   'UNIQUE '
                                                                                                   'INDEX '
                                                                                                   'uq_commerce_runtime_delivery_receipts_order '
                                                                                                   'ON '
                                                                                                   'public.commerce_runtime_delivery_receipts '
                                                                                                   'USING '
                                                                                                   'btree '
                                                                                                   '(attempt_id, '
                                                                                                   'receipt_no)'}}}
_EXPECTED_TRIGGER_DEFS = {'commerce_runtime_effect_attempts': 'CREATE TRIGGER trg_commerce_runtime_ledger_immutable BEFORE DELETE OR '
                                     'UPDATE ON public.commerce_runtime_effect_attempts FOR EACH ROW EXECUTE '
                                     'FUNCTION commerce_runtime_ledger_rows_immutable()',
 'commerce_runtime_effect_results': 'CREATE TRIGGER trg_commerce_runtime_ledger_immutable BEFORE DELETE OR '
                                    'UPDATE ON public.commerce_runtime_effect_results FOR EACH ROW EXECUTE '
                                    'FUNCTION commerce_runtime_ledger_rows_immutable()',
 'commerce_runtime_delivery_attempts': 'CREATE TRIGGER trg_commerce_runtime_ledger_immutable BEFORE DELETE '
                                       'OR UPDATE ON public.commerce_runtime_delivery_attempts FOR EACH ROW '
                                       'EXECUTE FUNCTION commerce_runtime_ledger_rows_immutable()',
 'commerce_runtime_delivery_receipts': 'CREATE TRIGGER trg_commerce_runtime_ledger_immutable BEFORE DELETE '
                                       'OR UPDATE ON public.commerce_runtime_delivery_receipts FOR EACH ROW '
                                       'EXECUTE FUNCTION commerce_runtime_ledger_rows_immutable()'}
_EXPECTED_FUNCTION_BODY = "BEGIN RAISE EXCEPTION '% rows are append-only', TG_TABLE_NAME USING ERRCODE = 'integrity_constraint_violation'; END"

_TRIGGER_FUNCTION_SQL = (
    f"CREATE OR REPLACE FUNCTION {_TRIGGER_FUNCTION}() RETURNS trigger "
    "LANGUAGE plpgsql AS $$ BEGIN "
    "RAISE EXCEPTION '% rows are append-only', TG_TABLE_NAME "
    "USING ERRCODE = 'integrity_constraint_violation'; END $$;"
)


def _trigger_sql(table: str) -> str:
    return (
        f"CREATE TRIGGER {_TRIGGER} BEFORE UPDATE OR DELETE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION {_TRIGGER_FUNCTION}();"
    )


def _tenant_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant")


def _conversation_scope_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["conversation_id", "tenant_id", "namespace"],
        [f"{_CONVERSATIONS}.id", f"{_CONVERSATIONS}.tenant_id", f"{_CONVERSATIONS}.namespace"],
        name=f"fk_{table}_conversation_scope",
    )


def _turn_scope_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["turn_id", "tenant_id", "namespace"],
        [f"{_TURNS}.id", f"{_TURNS}.tenant_id", f"{_TURNS}.namespace"],
        name=f"fk_{table}_turn_scope",
    )


def _timestamps() -> list:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    ]


def _create_effects() -> None:
    op.create_table(
        _EFFECTS,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("namespace", sa.String(length=16), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("turn_id", sa.BigInteger(), nullable=False),
        sa.Column("action_type", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'reserved'")),
        sa.Column("attempt_count", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("reserved_by", sa.String(length=128), nullable=False),
        sa.Column("reserved_fence", sa.BigInteger(), nullable=False),
        sa.Column("reserved_epoch", sa.BigInteger(), nullable=False),
        sa.Column("confirmed_result", postgresql.JSONB(), nullable=True),
        *_timestamps(),
        _tenant_fk(_EFFECTS),
        _conversation_scope_fk(_EFFECTS),
        _turn_scope_fk(_EFFECTS),
        sa.UniqueConstraint("tenant_id", "namespace", "idempotency_key", name="uq_commerce_runtime_effects_key"),
        sa.UniqueConstraint("id", "tenant_id", "namespace", "conversation_id", name="uq_commerce_runtime_effects_scope"),
        sa.CheckConstraint(_NAMESPACE_SQL, name="ck_commerce_runtime_effects_namespace"),
        sa.CheckConstraint(
            "status IN ('reserved', 'dispatching', 'confirmed', 'rejected', 'unknown')",
            name="ck_commerce_runtime_effects_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_commerce_runtime_effects_attempt_count"),
        sa.CheckConstraint(
            "(status = 'confirmed') = (confirmed_result IS NOT NULL)",
            name="ck_commerce_runtime_effects_confirmed_pair",
        ),
    )


def _create_effect_attempts() -> None:
    op.create_table(
        _EFFECT_ATTEMPTS,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("effect_id", sa.BigInteger(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("namespace", sa.String(length=16), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("attempt_no", sa.BigInteger(), nullable=False),
        sa.Column("dispatch_key", sa.String(length=64), nullable=False),
        sa.Column("reserved_by", sa.String(length=128), nullable=False),
        sa.Column("reserved_fence", sa.BigInteger(), nullable=False),
        sa.Column("reserved_epoch", sa.BigInteger(), nullable=False),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(
            ["effect_id", "tenant_id", "namespace", "conversation_id"],
            [f"{_EFFECTS}.id", f"{_EFFECTS}.tenant_id", f"{_EFFECTS}.namespace", f"{_EFFECTS}.conversation_id"],
            name="fk_commerce_runtime_effect_attempts_effect_scope",
        ),
        sa.UniqueConstraint("effect_id", "attempt_no", name="uq_commerce_runtime_effect_attempts_order"),
        sa.UniqueConstraint("dispatch_key", name="uq_commerce_runtime_effect_attempts_dispatch_key"),
        sa.UniqueConstraint("id", "effect_id", name="uq_commerce_runtime_effect_attempts_scope"),
        sa.CheckConstraint(_NAMESPACE_SQL, name="ck_commerce_runtime_effect_attempts_namespace"),
        sa.CheckConstraint("attempt_no >= 1", name="ck_commerce_runtime_effect_attempts_attempt_no"),
    )


def _create_effect_results() -> None:
    op.create_table(
        _EFFECT_RESULTS,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("attempt_id", sa.BigInteger(), nullable=False),
        sa.Column("effect_id", sa.BigInteger(), nullable=False),
        sa.Column("result_no", sa.BigInteger(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("recorded_by", sa.String(length=128), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(
            ["attempt_id", "effect_id"], [f"{_EFFECT_ATTEMPTS}.id", f"{_EFFECT_ATTEMPTS}.effect_id"],
            name="fk_commerce_runtime_effect_results_attempt_scope",
        ),
        sa.UniqueConstraint("attempt_id", "result_no", name="uq_commerce_runtime_effect_results_order"),
        sa.CheckConstraint("result_no >= 1", name="ck_commerce_runtime_effect_results_result_no"),
        sa.CheckConstraint(
            "outcome IN ('confirmed', 'rejected', 'unknown')", name="ck_commerce_runtime_effect_results_outcome",
        ),
    )


def _create_delivery_sequences() -> None:
    op.create_table(
        _DELIVERY_SEQUENCES,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("namespace", sa.String(length=16), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("turn_id", sa.BigInteger(), nullable=False),
        sa.Column("intent_kind", sa.String(length=16), nullable=False),
        sa.Column("intent_payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("intent_hash", sa.String(length=64), nullable=False),
        sa.Column("attempt_count", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("outcome", sa.String(length=32), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("reserved_by", sa.String(length=128), nullable=False),
        sa.Column("reserved_fence", sa.BigInteger(), nullable=False),
        sa.Column("reserved_epoch", sa.BigInteger(), nullable=False),
        *_timestamps(),
        _tenant_fk(_DELIVERY_SEQUENCES),
        _conversation_scope_fk(_DELIVERY_SEQUENCES),
        _turn_scope_fk(_DELIVERY_SEQUENCES),
        sa.UniqueConstraint("turn_id", name="uq_commerce_runtime_delivery_sequences_turn"),
        sa.UniqueConstraint(
            "id", "tenant_id", "namespace", "conversation_id", name="uq_commerce_runtime_delivery_sequences_scope",
        ),
        sa.CheckConstraint(_NAMESPACE_SQL, name="ck_commerce_runtime_delivery_sequences_namespace"),
        sa.CheckConstraint("intent_kind IN ('rich', 'text')", name="ck_commerce_runtime_delivery_sequences_intent_kind"),
        sa.CheckConstraint(
            "outcome IN ('pending', 'accepted', 'rejected', 'unknown')",
            name="ck_commerce_runtime_delivery_sequences_outcome",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_commerce_runtime_delivery_sequences_attempt_count"),
    )


def _create_delivery_attempts() -> None:
    op.create_table(
        _DELIVERY_ATTEMPTS,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("sequence_id", sa.BigInteger(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("namespace", sa.String(length=16), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("attempt_no", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("dispatch_key", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("reserved_by", sa.String(length=128), nullable=False),
        sa.Column("reserved_fence", sa.BigInteger(), nullable=False),
        sa.Column("reserved_epoch", sa.BigInteger(), nullable=False),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(
            ["sequence_id", "tenant_id", "namespace", "conversation_id"],
            [f"{_DELIVERY_SEQUENCES}.id", f"{_DELIVERY_SEQUENCES}.tenant_id", f"{_DELIVERY_SEQUENCES}.namespace",
             f"{_DELIVERY_SEQUENCES}.conversation_id"],
            name="fk_commerce_runtime_delivery_attempts_sequence_scope",
        ),
        sa.UniqueConstraint("sequence_id", "attempt_no", name="uq_commerce_runtime_delivery_attempts_order"),
        sa.UniqueConstraint("dispatch_key", name="uq_commerce_runtime_delivery_attempts_dispatch_key"),
        sa.UniqueConstraint("id", "sequence_id", name="uq_commerce_runtime_delivery_attempts_scope"),
        sa.CheckConstraint(_NAMESPACE_SQL, name="ck_commerce_runtime_delivery_attempts_namespace"),
        sa.CheckConstraint("kind IN ('rich', 'text')", name="ck_commerce_runtime_delivery_attempts_kind"),
        sa.CheckConstraint("attempt_no >= 1", name="ck_commerce_runtime_delivery_attempts_attempt_no"),
    )


def _create_delivery_receipts() -> None:
    op.create_table(
        _DELIVERY_RECEIPTS,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("attempt_id", sa.BigInteger(), nullable=False),
        sa.Column("sequence_id", sa.BigInteger(), nullable=False),
        sa.Column("receipt_no", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("provider_message_id", sa.String(length=256), nullable=True),
        sa.Column("evidence", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("recorded_by", sa.String(length=128), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(
            ["attempt_id", "sequence_id"], [f"{_DELIVERY_ATTEMPTS}.id", f"{_DELIVERY_ATTEMPTS}.sequence_id"],
            name="fk_commerce_runtime_delivery_receipts_attempt_scope",
        ),
        sa.UniqueConstraint("attempt_id", "receipt_no", name="uq_commerce_runtime_delivery_receipts_order"),
        sa.CheckConstraint("receipt_no >= 1", name="ck_commerce_runtime_delivery_receipts_receipt_no"),
        sa.CheckConstraint(
            "kind IN ('accepted', 'rejected', 'unknown', 'delivered', 'read', 'failed')",
            name="ck_commerce_runtime_delivery_receipts_kind",
        ),
        sa.CheckConstraint(
            "kind <> 'accepted' OR provider_message_id IS NOT NULL",
            name="ck_commerce_runtime_delivery_receipts_accepted_id",
        ),
    )


_CREATORS = (
    (_EFFECTS, _create_effects),
    (_EFFECT_ATTEMPTS, _create_effect_attempts),
    (_EFFECT_RESULTS, _create_effect_results),
    (_DELIVERY_SEQUENCES, _create_delivery_sequences),
    (_DELIVERY_ATTEMPTS, _create_delivery_attempts),
    (_DELIVERY_RECEIPTS, _create_delivery_receipts),
)


def _norm(text_value: str) -> str:
    return " ".join(str(text_value or "").split())


def _table_regclass(table: str) -> str:
    return f"public.{table}"


def _actual_columns(bind, table: str):
    rows = bind.execute(sa.text(
        "SELECT column_name, udt_name, character_maximum_length, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_schema = 'public' AND table_name = :t "
        "ORDER BY ordinal_position"), {"t": table}).all()
    return {r[0]: (r[1], r[2], r[3], r[4]) for r in rows}


def _actual_constraints(bind, table: str):
    # PostgreSQL 18 also records NOT NULL in pg_constraint (contype='n').
    # Nullability is compared for every column by _actual_columns, including
    # unexpected tightening. Do not compare generated NOT NULL names with the
    # declared table constraints. An unvalidated constraint is still refused.
    rows = bind.execute(sa.text(
        "SELECT conname, contype, pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = CAST(:t AS regclass) "
        "AND (contype <> 'n' OR NOT convalidated)"), {"t": _table_regclass(table)}).all()
    return {r[0]: (str(r[1]), _norm(r[2])) for r in rows}


def _actual_indexes(bind, table: str):
    rows = bind.execute(sa.text(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename = :t"),
        {"t": table}).all()
    return {r[0]: _norm(r[1]) for r in rows}


def _trigger_row(bind, table: str):
    """The append-only trigger on the given ledger relation itself, or None."""
    return bind.execute(sa.text(
        "SELECT t.tgname, t.tgenabled, p.proname, pg_get_triggerdef(t.oid) "
        "FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid "
        "WHERE NOT t.tgisinternal AND t.tgrelid = CAST(:t AS regclass) AND t.tgname = :name"),
        {"t": _table_regclass(table), "name": _TRIGGER}).one_or_none()


def _function_bodies(bind):
    rows = bind.execute(sa.text(
        "SELECT p.prosrc FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' AND p.proname = :name"), {"name": _TRIGGER_FUNCTION}).all()
    return [_norm(r[0]) for r in rows]


def trigger_differences(bind) -> list:
    """Empty when every append-only trigger is present, enabled and defined as expected."""
    diffs = []
    for table in _APPEND_ONLY:
        if not has_table(bind, table):
            continue
        row = _trigger_row(bind, table)
        if row is None:
            diffs.append(f"{table}: trigger {_TRIGGER} absent on the ledger relation")
            continue
        if str(row[1]) not in ("O", "A"):
            diffs.append(f"{table}: trigger {_TRIGGER} is not enabled (tgenabled={row[1]})")
        if row[2] != _TRIGGER_FUNCTION:
            diffs.append(f"{table}: trigger {_TRIGGER} executes {row[2]}, expected {_TRIGGER_FUNCTION}")
        if _norm(row[3]) != _norm(_EXPECTED_TRIGGER_DEFS[table]):
            diffs.append(f"{table}: trigger definition differs: {row[3]}")
    bodies = _function_bodies(bind)
    if not bodies or any(body != _norm(_EXPECTED_FUNCTION_BODY) for body in bodies):
        diffs.append(f"function {_TRIGGER_FUNCTION} absent or its body differs from this revision")
    return diffs


def schema_differences(bind, *, include_trigger: bool = True) -> list:
    """Every difference between the live schema and this revision's definition.

    Absent tables are reported as differences too; ``upgrade`` creates them
    before calling this, so on a successful upgrade the list is empty.
    """
    diffs = []
    for table, expected in _EXPECTED.items():
        if not has_table(bind, table):
            diffs.append(f"{table}: table absent")
            continue
        actual_cols = _actual_columns(bind, table)
        expected_cols = {c[0]: (c[1], c[2], c[3], c[4]) for c in expected["columns"]}
        for name, spec in expected_cols.items():
            if name not in actual_cols:
                diffs.append(f"{table}.{name}: column absent")
            elif actual_cols[name] != spec:
                diffs.append(f"{table}.{name}: (udt, length, nullable, default) is {actual_cols[name]}, expected {spec}")
        for name in actual_cols:
            if name not in expected_cols:
                diffs.append(f"{table}.{name}: unexpected column")
        actual_cons = _actual_constraints(bind, table)
        expected_cons = {k: (v[0], _norm(v[1])) for k, v in expected["constraints"].items()}
        for name, spec in expected_cons.items():
            if name not in actual_cons:
                diffs.append(f"{table}: constraint {name} absent")
            elif actual_cons[name] != spec:
                diffs.append(f"{table}: constraint {name} is {actual_cons[name]}, expected {spec}")
        for name in actual_cons:
            if name not in expected_cons:
                diffs.append(f"{table}: unexpected constraint {name}")
        actual_idx = _actual_indexes(bind, table)
        expected_idx = {k: _norm(v) for k, v in expected["indexes"].items()}
        for name, spec in expected_idx.items():
            if name not in actual_idx:
                diffs.append(f"{table}: index {name} absent")
            elif actual_idx[name] != spec:
                diffs.append(f"{table}: index {name} is {actual_idx[name]}, expected {spec}")
        for name in actual_idx:
            if name not in expected_idx:
                diffs.append(f"{table}: unexpected index {name}")
    if include_trigger and all(has_table(bind, t) for t in _APPEND_ONLY):
        diffs.extend(trigger_differences(bind))
    return diffs


class IncompatibleSchema(RuntimeError):
    """A pre-existing shape this revision refuses to reconcile or stamp."""


def upgrade() -> None:
    bind = op.get_bind()
    for table in (_CONVERSATIONS, _TURNS):
        if not has_table(bind, table):
            raise IncompatibleSchema(f"0109 requires revision 0108's table {table}; it is absent")

    for table, create in _CREATORS:
        if not has_table(bind, table):
            create()

    for table, index_name, columns in _INDEXES:
        if not has_index(bind, table, index_name):
            op.create_index(index_name, table, columns)

    # Verify every pre-existing (or just created) table by definition. A
    # difference is refused explicitly; nothing is stamped.
    diffs = schema_differences(bind, include_trigger=False)
    if diffs:
        raise IncompatibleSchema(
            "0109 refuses to reconcile an incompatible pre-existing schema: " + "; ".join(diffs)
        )

    if bind.dialect.name == "postgresql":
        bodies = _function_bodies(bind)
        if not bodies:
            op.execute(sa.text(_TRIGGER_FUNCTION_SQL))
        elif any(body != _norm(_EXPECTED_FUNCTION_BODY) for body in bodies):
            raise IncompatibleSchema(
                f"0109 refuses to replace an existing function {_TRIGGER_FUNCTION} whose body differs"
            )
        for table in _APPEND_ONLY:
            if _trigger_row(bind, table) is None:
                op.execute(sa.text(_trigger_sql(table)))
        trigger_diffs = trigger_differences(bind)
        if trigger_diffs:
            raise IncompatibleSchema(
                "0109 refuses an incompatible append-only trigger: " + "; ".join(trigger_diffs)
            )
        remaining = schema_differences(bind, include_trigger=True)
        if remaining:
            raise IncompatibleSchema("0109 post-condition failed: " + "; ".join(remaining))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table in _APPEND_ONLY:
            if has_table(bind, table):
                op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_TRIGGER} ON {table}"))
    for table in (_DELIVERY_RECEIPTS, _DELIVERY_ATTEMPTS, _DELIVERY_SEQUENCES,
                  _EFFECT_RESULTS, _EFFECT_ATTEMPTS, _EFFECTS):
        if has_table(bind, table):
            op.drop_table(table)
    if bind.dialect.name == "postgresql":
        # Drop the function only when no other trigger still executes it.
        dependants = bind.execute(sa.text(
            "SELECT count(*) FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid "
            "WHERE NOT t.tgisinternal AND p.proname = :name"), {"name": _TRIGGER_FUNCTION}).scalar()
        if not dependants:
            op.execute(sa.text(f"DROP FUNCTION IF EXISTS {_TRIGGER_FUNCTION}()"))
