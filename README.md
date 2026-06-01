An out of band host (+ device TBD) memory logger for forge workloads

Measures:
- Process RSS over time
- OOM score over time
- Swap usage over time
- (TBD) device DRAM usage over time by reading shm regions exposed by https://github.com/tenstorrent/tt-metal/pull/37218

Outputs:
plotly html:

<img width="1895" height="440" alt="image" src="https://github.com/user-attachments/assets/c3a4b18b-ed3b-4134-b9a8-4c9a9483f2a4" />



png and example analysis:

<img width="760" height="619" alt="image" src="https://github.com/user-attachments/assets/510a0b5a-6673-4346-8ccd-1a858658b209" />

