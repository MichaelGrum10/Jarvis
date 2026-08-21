-- Search the inbox by subject and sender.
--
--   osascript mail_search.applescript "lease renewal" 5
--
-- Subject and sender only, deliberately. Mail can search message bodies with
-- `whose content contains`, but it does so by pulling every message through an
-- Apple event one at a time — minutes on a real mailbox, against a caller that
-- gives up after twenty seconds. A subject-and-sender search that returns is
-- worth more than a full-text one that times out.
--
-- The search term arrives as an argument to the run handler. It is never part
-- of this script's source, so there is nothing for it to escape out of.

on run argv
	set q to (item 1 of argv) as text
	set wanted to (item 2 of argv) as integer

	set fieldSep to ASCII character 31
	set recSep to ASCII character 30
	set output to ""

	with timeout of 15 seconds
		tell application "Mail"
			set box to inbox
			set matches to (messages of box whose (subject contains q) or (sender contains q))
			set found to (count of matches)
			if found < wanted then set wanted to found

			repeat with i from 1 to wanted
				set msg to item i of matches
				set d to date received of msg
				set output to output & ((message id of msg) as text)
				set output to output & fieldSep & ((subject of msg) as text)
				set output to output & fieldSep & ((sender of msg) as text)
				set output to output & fieldSep & ((read status of msg) as text)
				set output to output & fieldSep & my stamp(d)
				set output to output & recSep
			end repeat
		end tell
	end timeout

	return output
end run

on stamp(d)
	set m to (month of d) as integer
	return ((year of d) as text) & "," & (m as text) & "," & ((day of d) as text) & "," & ((hours of d) as text) & "," & ((minutes of d) as text)
end stamp
