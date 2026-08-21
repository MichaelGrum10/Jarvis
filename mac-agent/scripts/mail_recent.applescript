-- The most recent messages in Mail.app's unified inbox.
--
--   osascript mail_recent.applescript 5
--
-- Same delimiters as calendar_list.applescript: ASCII 31 between fields, ASCII
-- 30 between records, numeric date components. Subject lines contain absolutely
-- anything, so the delimiters have to be characters a subject cannot hold.
--
-- Properties are fetched in bulk (`subject of msgs`, one Apple event returning
-- a list) rather than per message in a loop. Mail charges for every Apple event
-- round trip, and asking fifty times in a loop is the difference between a
-- second and half a minute.

on run argv
	set wanted to (item 1 of argv) as integer

	set fieldSep to ASCII character 31
	set recSep to ASCII character 30
	set output to ""

	with timeout of 15 seconds
		tell application "Mail"
			set box to inbox
			set total to (count of messages of box)
			if total < wanted then set wanted to total

			if wanted > 0 then
				set msgs to messages 1 thru wanted of box
				set theIds to message id of msgs
				set theSubjects to subject of msgs
				set theSenders to sender of msgs
				set theRead to read status of msgs
				set theDates to date received of msgs

				repeat with i from 1 to wanted
					set d to item i of theDates
					set output to output & ((item i of theIds) as text)
					set output to output & fieldSep & ((item i of theSubjects) as text)
					set output to output & fieldSep & ((item i of theSenders) as text)
					set output to output & fieldSep & ((item i of theRead) as text)
					set output to output & fieldSep & my stamp(d)
					set output to output & recSep
				end repeat
			end if
		end tell
	end timeout

	return output
end run

on stamp(d)
	set m to (month of d) as integer
	return ((year of d) as text) & "," & (m as text) & "," & ((day of d) as text) & "," & ((hours of d) as text) & "," & ((minutes of d) as text)
end stamp
