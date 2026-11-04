"""Test harness: stock Graphiti on Neo4j with the photon data (EMBER-146). See docs/research/graphiti-test.md."""

import os

# Here, not at run time, so that importing any module of this package turns it off before graphiti_core loads.
# Telemetry defaults to on and posts to PostHog from every Graphiti().
os.environ["GRAPHITI_TELEMETRY_ENABLED"] = "false"
