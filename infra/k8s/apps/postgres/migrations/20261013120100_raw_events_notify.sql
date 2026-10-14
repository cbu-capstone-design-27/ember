-- EMBER-75, interim: NOTIFY on every raw_events insert, so enrich workers wake within a second.
-- EMBER-62 (intake) may replace it: a later migration drops trigger and function raw_events_notify
-- and adds its own. Workers rely only on the channel, raw_events, which they can override with
-- RAW_EVENTS_CHANNEL. They ignore the payload, so its format is free to change.

-- migrate:up
create function raw_events_notify() returns trigger language plpgsql as $$
begin
  perform pg_notify('raw_events', new.source);
  return null;
end
$$;

create trigger raw_events_notify after insert on raw_events
  for each row execute function raw_events_notify();

-- migrate:down
drop trigger raw_events_notify on raw_events;
drop function raw_events_notify();
