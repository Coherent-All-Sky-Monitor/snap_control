#!/bin/bash

# 1. Initialize Conda for this script's shell
# (Adjust this path if your anaconda is installed elsewhere)
source /home/ubuntu/anaconda3/etc/profile.d/conda.sh

# 2. Activate the environment
conda activate 

# 3. Run the python script
# Using 'exec' ensures the python process replaces this shell script 
# (This helps systemd handle signals/stopping the service correctly)
exec python start_snaps.py
```

**2. Make it executable**
```bash
chmod +x /home/user/software/snap_control/run_snap.sh