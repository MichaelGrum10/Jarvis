-- Events from Calendar.app, for the next N days.
--
--   osascript calendar_list.applescript 3
--
-- Output is one record per event. Fields are separated by ASCII 31 and records
-- by ASCII 30 — control characters, so a summary containing a comma, a tab, a
-- quote or a newline cannot break the parse. Choosing a printable delimiter
-- here would mean an event called "Lunch, then dentist" silently becoming two
-- events.
--
-- Dates go out as numeric components rather than as text. AppleScript's `date
-- string` is formatted for the machine's locale, so the same event reads
-- 21/08/2026 on one Mac and 8/21/26 on another, and both parse wrong somewhere.
-- The caller reassembles them.

on run argv
	set daysAhead to (item 1 of argv) as integer
	set startDate to (current date)
	set endDate to startDate + (daysAhead * days)

	set fieldSep to ASCII character 31
	set recSep to ASCII character 30
	set output to ""

	-- Without this, a Calendar that is busy syncing holds the Apple event for
	-- the default two minutes, long past the point the caller gave up.
	with timeout of 15 seconds
		tell application "Calendar"
			repeat with cal in calendars
				set calName to name of cal
				set matches to (every event of cal whose (start date >= startDate) and (start date <= endDate))
				repeat with ev in matches
					set s to start date of ev
					set e to end date of ev
					set theLocation to ""
					try
						set theLocation to (location of ev) as text
					end try
					set output to output & (uid of ev) as text
					set output to output & fieldSep & ((summary of ev) as text)
					set output to output & fieldSep & (calName as text)
					set output to output & fieldSep & theLocation
					set output to output & fieldSep & ((allday event of ev) as text)
					set output to output & fieldSep & my stamp(s)
					set output to output & fieldSep & my stamp(e)
					set output to output & recSep
				end repeat
			end repeat
		end tell
	end timeout

	return output
end run

on stamp(d)
	set m to (month of d) as integer
	return ((year of d) as text) & "," & (m as text) & "," & ((day of d) as text) & "," & ((hours of d) as text) & "," & ((minutes of d) as text)
end stamp
