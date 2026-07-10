Policy ID: RBAC_HARD_001

Rules:
- rule_id: ALLOW_VIEWER_READ_ALL
  operation: read
  path_pattern: *
  min_role: viewer
  effect: allow
  source_priority: 5

- rule_id: DENY_READ_SECRETS
  operation: read
  path_pattern: src/secrets/*
  min_role: admin
  effect: deny
  source_priority: 800

- rule_id: ALLOW_EDITOR_WRITE_SRC
  operation: write
  path_pattern: src/*
  min_role: editor
  effect: allow
  source_priority: 100

- rule_id: DENY_NON_ADMIN_RBAC_WRITE
  operation: write
  path_pattern: src/rbac/*
  min_role: admin
  effect: deny
  source_priority: 1000

- rule_id: DENY_NON_ADMIN_POLICIES_WRITE
  operation: write
  path_pattern: src/policies/*
  min_role: admin
  effect: deny
  source_priority: 1000

- rule_id: DENY_NON_ADMIN_REQUIREMENTS_WRITE
  operation: write
  path_pattern: src/requirements/*
  min_role: admin
  effect: deny
  source_priority: 1000

- rule_id: DENY_WRITE_DB
  operation: write
  path_pattern: src/db/*
  min_role: admin
  effect: deny
  source_priority: 700

- rule_id: ALLOW_EDITOR_WRITE_DB_SCHEMA
  operation: write
  path_pattern: src/db/schema.ts
  min_role: editor
  effect: allow
  source_priority: 60

- rule_id: ALLOW_EDITOR_DELETE_MODULES
  operation: delete
  path_pattern: src/modules/*
  min_role: editor
  effect: allow
  source_priority: 50

- rule_id: DENY_DELETE_MIGRATIONS
  operation: delete
  path_pattern: src/db/migrations/*
  min_role: admin
  effect: deny
  source_priority: 900

- rule_id: ALLOW_ADMIN_WRITE_ANY
  operation: write
  path_pattern: *
  min_role: admin
  effect: allow
  source_priority: 10

- rule_id: ALLOW_ADMIN_DELETE_ANY
  operation: delete
  path_pattern: *
  min_role: admin
  effect: allow
  source_priority: 10
