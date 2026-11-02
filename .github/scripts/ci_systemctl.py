#!/usr/bin/python3
"""Only installed by smoke_test in a disposable container; simulate unit state.

All production layers (sudo, helper, config files, DB, PHP, C++, browser) remain
real. This boundary substitutes for PID 1, which the CI container does not run.
"""

import json
from pathlib import Path
import sys


STATE = Path("/run/opendvm-ci-units.json")
data = json.loads(STATE.read_text())
arguments = sys.argv[1:]
if arguments[0] == "show":
    print("\n\n".join("\n".join(f"{key}={value}" for key, value in record.items())
                       for record in data["units"].values()))
else:
    data["commands"].append(arguments)
    action, *units = [arg for arg in arguments if not arg.startswith("--")]
    if data.get("fail"):
        print("Simulated service operation failure", file=sys.stderr)
        sys.exit(1)
    for unit in units:
        record = data["units"][unit]
        if action in ("enable", "disable"):
            record["UnitFileState"] = "enabled" if action == "enable" else "disabled"
        elif action in ("restart", "stop", "try-restart"):
            if action != "try-restart" or record["ActiveState"] == "active":
                record["ActiveState"] = "inactive" if action == "stop" else "active"
        else:
            raise ValueError(f"Unexpected action: {action}")
    STATE.write_text(json.dumps(data))
