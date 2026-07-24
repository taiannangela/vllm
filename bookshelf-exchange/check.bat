@echo off
rem Connection check-up: gathers the facts needed to debug "site can't be
rem reached from my phone". Run while the site (run.bat) is running, then
rem copy everything it prints into the chat.
echo ================= Bookshelf Exchange check-up =================
echo.
echo [1] This PC's network addresses (phone should use the 192.168.x.x one):
ipconfig | findstr /i "IPv4"
echo.
echo [2] Is the site running, and is it open to the network?
netstat -an | findstr ":5000" | findstr /i "LISTENING"
if errorlevel 1 (
    echo     ^>^> NOTHING on port 5000 - the site is NOT running.
    echo     ^>^> Start it with run.bat in another window, then rerun this.
) else (
    echo     ^>^> If the line above starts with "TCP    0.0.0.0:5000" the
    echo     ^>^> site is open to your Wi-Fi. If it says "127.0.0.1:5000"
    echo     ^>^> it is PC-only - run update.bat then restart run.bat.
)
echo.
echo [3] Windows Firewall rules for Python (phone access needs an Allow rule):
netsh advfirewall firewall show rule name=all dir=in status=enabled | findstr /i /c:"Rule Name:" | findstr /i "python"
if errorlevel 1 echo     ^>^> No Python rules found - firewall is likely blocking the phone.
echo.
echo [4] Active firewall profile:
netsh advfirewall show currentprofile | findstr /i "Profile State"
echo.
echo ===============================================================
echo Copy EVERYTHING above and paste it into the chat.
pause
