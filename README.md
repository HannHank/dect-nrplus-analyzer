# dect-nrplus-analyzer 

This is a very simple spectrum analyzer that lets you check the occupancy of selected `DECTNR+` channels unsing one **nrf9151-dk**. 
I developed most of it during the dect-world-2026 in Munich, where I took my first steps with [hophop](https://github.com/ariel-os/hophop).

To keep my focus on the core Rust implementation and learning hophop, I leaned on Claude to build the graphical terminal application. It works for now, but will definitely need some manual validation later.

## Running the analyzer

```bash
laze build -b nrf9151-dk run | pipx run ./channel-analyzer.py -
```
