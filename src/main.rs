#![no_main]
#![no_std]

use ariel_os::log::{info, warn};

use ts_103_636_utils::identifiers::{AbsoluteChannel, NetworkId32};

const CHANNELS: [AbsoluteChannel; 11] = [
    AbsoluteChannel::new(1657).unwrap(),
    AbsoluteChannel::new(1659).unwrap(),
    AbsoluteChannel::new(1661).unwrap(),
    AbsoluteChannel::new(1663).unwrap(),
    AbsoluteChannel::new(1665).unwrap(),
    AbsoluteChannel::new(1667).unwrap(),
    AbsoluteChannel::new(1669).unwrap(),
    AbsoluteChannel::new(1671).unwrap(),
    AbsoluteChannel::new(1673).unwrap(),
    AbsoluteChannel::new(1675).unwrap(),
    AbsoluteChannel::new(1677).unwrap(),
];

// operation the modem accepts → try and error (33 out of memory)
const MAX_QUEUED: usize = 32;

const _: () = assert!(
    CHANNELS.len() <= MAX_QUEUED,
    "The modem does not take that many operations at once"
);

/// RSSI samples per subslot: A framess report has 240 samples, for 48 subslots.
const SAMPLES_PER_SUBSLOT: usize = 5;

/// Beacons are received from any network, but other packets only from this one
const NETWORK_ID: NetworkId32 = NetworkId32::new(0x87654321).unwrap();

#[ariel_os::task(autostart)]
async fn main() {
    let mut dect = hophop::nrfxlib_phy::DectPhy::init_after_modem_init(())
        .await
        .unwrap();

    info!("Sweeping {} channels.", CHANNELS.len());

    loop {
        if let Err(e) = dect
            .rssi_bulk(
                &CHANNELS,
                Some(NETWORK_ID),
                async |result| {
                    let Some(result) = result else {
                        warn!("RSSI missed.");
                        return;
                    };
                    // Printing all 240 samples takes longer than the frame they were measured
                    let mut subslots = [0; 48];
                    for (subslot, samples) in subslots
                        .iter_mut()
                        .zip(result.data().chunks(SAMPLES_PER_SUBSLOT))
                    {
                        *subslot = strongest(samples);
                    }
                    info!(
                        "RSSI for {} at {}: {:?}",
                        result.carrier(),
                        result.start_time(),
                        subslots
                    );
                },
                async |received| match received {
                    Ok(recv) => info!(
                        "Data received: time {}, PCC {:?}, PDC length {:?}, RSSI {} dBm, SNR {} dB.",
                        recv.pcc.time,
                        recv.pcc(),
                        recv.pdc().map(<[u8]>::len),
                        recv.pcc.rssi_dbm(),
                        recv.pcc.snr_db()
                    ),
                    Err(e) => warn!("Receive error: {:?}", e),
                },
            )
            .await
        {
            // typically → a channels turn came before it could be set up 
            warn!("Sweep incomplete: {:?}", e);
        }
    }
}

/// the strongest of some RSSI samples, or 0 if any of them was not measured → because a packet was received in that subslot
fn strongest(samples: &[u8]) -> u8 {
    if samples.contains(&0) {
        return 0;
    }
    samples.iter().map(|&s| s as i8).max().unwrap_or(0) as u8
}
