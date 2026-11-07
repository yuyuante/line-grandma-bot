' Start the bot with no console window (0 = hidden, False = do not wait). Logs still go to logs/.
' No console = no prompt, so auto-shutdown is off here (--stop-after 0); pass --stop-after N to enable.
' Stop it: send the stop command in LINE, or end the python.exe running auto_reply.py in Task Manager.
Set sh = CreateObject("WScript.Shell")
dir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
sh.Run "cmd /c chcp 65001 >nul & python auto_reply.py --stop-after 0", 0, False
