// Ember-specific constraints. Idempotent; safe to re-run.
// Graphiti builds its own indexes with build_indices_and_constraints(); run that first.
// Neo4j Community: uniqueness constraints are supported, existence and node-key are not.
// Apply after the EMBER-34 spike confirms Graphiti stores custom attributes as node properties.

CREATE CONSTRAINT ember_config_key IF NOT EXISTS FOR (n:EmberConfig) REQUIRE n.key IS UNIQUE;

CREATE CONSTRAINT identity_source_key IF NOT EXISTS FOR (n:Identity) REQUIRE (n.group_id, n.source, n.external_id) IS UNIQUE;
CREATE CONSTRAINT container_source_key IF NOT EXISTS FOR (n:Container) REQUIRE (n.group_id, n.source, n.external_id) IS UNIQUE;
CREATE CONSTRAINT workitem_source_key IF NOT EXISTS FOR (n:WorkItem) REQUIRE (n.group_id, n.source, n.external_id) IS UNIQUE;
CREATE CONSTRAINT change_source_key IF NOT EXISTS FOR (n:Change) REQUIRE (n.group_id, n.source, n.external_id) IS UNIQUE;
CREATE CONSTRAINT conversation_source_key IF NOT EXISTS FOR (n:Conversation) REQUIRE (n.group_id, n.source, n.external_id) IS UNIQUE;
CREATE CONSTRAINT message_source_key IF NOT EXISTS FOR (n:Message) REQUIRE (n.group_id, n.source, n.external_id) IS UNIQUE;

// Embedding identity record (ADR 0001). Written once at first startup from ontology.config.EXPECTED.
// MERGE (c:EmberConfig {key: 'embedding'})
//   ON CREATE SET c.model = $model, c.revision = $revision, c.dimension = $dimension,
//                 c.query_template = $query_template, c.schema_version = $schema_version;
