# dect-nrplus-analyzer 

This is a very simple spectrum analyzer that lets you check the occupancy of selected `DECTNR+` channels unsing one **nrf9151-dk**. 
I developed most of it during the dect-world-2026 in Munich, where I took my first steps with [hophop](https://github.com/ariel-os/hophop).

To keep my focus on the core Rust implementation and learning hophop, I leaned on Claude to build the graphical terminal application. It works for now, but will definitely need some manual validation later.

## Running the analyzer

```bash
laze build -b nrf9151-dk run | pipx run ./channel-analyzer.py -
```

## Example

<img width="1909" height="463" alt="image" src="https://github.com/user-attachments/assets/8caf2609-c886-4459-91a3-5ff88bd40843" />

This was captured in a controlled environment, where I could only disturb my FritzBox and the connected DECT devices. 
It is quite a nice example, because my demo is blasting over the band 1 (no LBT), and you can actually see how the FritzBox tries to find a free frequency and eventually 
manages to settle down at channel `1657`.

The 4% utilization of channel 1657 is also evidence that this is the FritzBox's beacon, because one beacon takes up 1 of the 24 slots per frame (~4%).
