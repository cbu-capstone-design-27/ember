-- EMBER-77: NOTIFY on every source_records insert, so the embed worker wakes within a second. The
-- worker relies only on the channel, source_records, which it can override with
-- SOURCE_RECORDS_CHANNEL. It ignores the payload, so its format is free to change.

-- migrate:up
create function source_records_notify() returns trigger language plpgsql as $$
begin
  perform pg_notify('source_records', new.source);
  return null;
end
$$;

create trigger source_records_notify after insert on source_records
  for each row execute function source_records_notify();

-- migrate:down
drop trigger source_records_notify on source_records;
drop function source_records_notify();
