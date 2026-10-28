-- EMBER-85: NOTIFY when an episode waits for load, so the load worker wakes within a second: on
-- insert, and when extract re-extracts an episode and clears its loaded_at. Load stamping loaded_at
-- sends nothing. The worker relies only on the channel, episodes, which it can override with
-- EPISODES_CHANNEL. It ignores the payload, so its format is free to change.

-- migrate:up
create function episodes_notify() returns trigger language plpgsql as $$
begin
  perform pg_notify('episodes', new.source);
  return null;
end
$$;

create trigger episodes_notify after insert or update of loaded_at on episodes
  for each row when (new.loaded_at is null) execute function episodes_notify();

-- migrate:down
drop trigger episodes_notify on episodes;
drop function episodes_notify();
