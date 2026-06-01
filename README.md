An out of band host (+ device TBD) memory logger for forge workloads

Measures:
- Process RSS over time
- OOM score over time
- Swap usage over time
- (TBD) device DRAM usage over time by reading shm regions exposed by https://github.com/tenstorrent/tt-metal/pull/37218

Outputs:
plotly html:
<img width="1911" height="525" alt="image" src="https://github.com/user-attachments/assets/52ec14f5-d418-4eca-8a16-868c5dd786b8" />

png and example analysis:
<img width="760" height="619" alt="image" src="https://github.com/user-attachments/assets/510a0b5a-6673-4346-8ccd-1a858658b209" />

