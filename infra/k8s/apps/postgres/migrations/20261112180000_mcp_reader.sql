-- EMBER-166: what the MCP server needs to read the graph as role ember_graph_reader.
--
-- ember_user_tenants(email): the tenants of every organization the user belongs to (their personal
-- workspace is one too, EMBER-165), from the web app's Better Auth tables in schema web. Security
-- definer, owned by ember, so the reader can resolve its scope without select on web.*. It is
-- plpgsql so it resolves web.* at call time: the web app creates those tables on its first start,
-- after the migrations, and before that the function returns no tenants.
--
-- The embed worker (services/embed) also embeds graph.nodes.embed_text and graph.edges.fact. These
-- triggers NOTIFY channel graph when a row needs it, so the worker wakes within a second, and clear
-- a node's embedding when its embed_text changes, so it never keeps the vector of an older text.

-- migrate:up
create function ember_user_tenants(user_email text) returns text[]
  language plpgsql stable security definer set search_path = pg_catalog, pg_temp as $$
begin
  if to_regclass('web.member') is null then
    return '{}';
  end if;
  return coalesce((select array_agg(distinct o.tenant order by o.tenant)
                     from web."user" u
                     join web.member m on m."userId" = u.id
                     join web.organization o on o.id = m."organizationId"
                    where lower(u.email) = lower(user_email)), '{}');
end
$$;
revoke execute on function ember_user_tenants(text) from public;
grant execute on function ember_user_tenants(text) to ember_graph_reader;

create function graph_nodes_embed() returns trigger language plpgsql as $$
begin
  if tg_op = 'UPDATE' and new.embed_text is distinct from old.embed_text
     and new.embedding is not distinct from old.embedding then
    new.embedding := null;
  end if;
  if new.embedding is null and new.embed_text is not null then
    perform pg_notify('graph', 'nodes');
  end if;
  return new;
end
$$;
create trigger graph_nodes_embed before insert or update of embed_text on graph.nodes
  for each row execute function graph_nodes_embed();

create function graph_edges_embed() returns trigger language plpgsql as $$
begin
  perform pg_notify('graph', 'edges');
  return null;
end
$$;
create trigger graph_edges_embed after insert on graph.edges
  for each row when (new.fact is not null and new.fact_embedding is null) execute function graph_edges_embed();

-- migrate:down
drop trigger graph_edges_embed on graph.edges;
drop function graph_edges_embed();
drop trigger graph_nodes_embed on graph.nodes;
drop function graph_nodes_embed();
drop function ember_user_tenants(text);
