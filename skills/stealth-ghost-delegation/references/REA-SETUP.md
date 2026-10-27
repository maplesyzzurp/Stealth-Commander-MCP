# REA installation

From this skill's directory, install the pinned browser-control dependency into a dedicated directory outside the skill:

```powershell
.\scripts\SETUP-REA.ps1 -InstallDirectory 'D:\Tools\stealth-hidden-desktop\rea'
```

The script installs `rea-agents@6.7.0` with package lifecycle scripts disabled. It requires Node.js 22.19+, 24.11+, or 26+ and npm. It keeps the npm cache and packages in the chosen directory, checks the package version and CLI help, and prints the entry path. It makes no global installation or agent registration. An existing destination must be empty or belong to an earlier successful run of this script.

The tested setup used Node.js 22.23.2 and npm 12.1.0. The published package includes its matching Windows native addon; no native build is needed for the browser workflow. The browser adapter itself does not require PyCharm. If Python dependencies are missing, install them into the selected Python environment:

```powershell
python -m pip install -r .\scripts\requirements.txt
```

Continue with [the browser adapter and viewer guide](REA-INTEGRATION.md). Use the adapter to select its owned browser automatically; do not manually connect REA to a personal browser.
