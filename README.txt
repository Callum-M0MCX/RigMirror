RigMirror v0.3.003
==================

HF-ONLY MULTI-MANUFACTURER CANDIDATE

Keep your working v0.3.002 folder. Extract this ZIP into a new folder.

v0.3.003 corrective change
--------------------------

When the live TX action changes from PARKING FREQUENCY or NO CHANGE to
SWITCH ANTENNA / RX PORT while Mirror is already running, RigMirror now
captures the Sub's normal receive path before the next transmission. On RX it
can therefore restore that exact path. Diversion is refused if no restoration
snapshot exists, preventing a Sub from being stranded on the diversion input.

Important operating boundary
----------------------------

RigMirror v0.3.003 mirrors only 30 kHz through 30.000 MHz. A Master above
30 MHz remains connected and displays its real frequency, but Sub tuning,
M and Split pause. The Sub readout turns grey, shows the requested Master
frequency and a red warning dot. Mirroring resumes automatically below
30 MHz. TX antenna/RX-port diversion or parking remains available.

TX protection choices
---------------------

Config offers exactly three choices for a separate Sub radio:

  NO CHANGE
  SWITCH ANTENNA / RX PORT (only where the driver supplies it)
  PARKING FREQUENCY

CAT acts only after Master TX has been detected. It is best-effort routing,
not guaranteed RF protection. The routing write is the first Sub command on
the TX edge. RigMirror does not poll whether the Sub has been manually keyed.

M correction
------------

On radios such as the TS-590, M now prepares the unused VFO and selects it
for transmit while the radio is still receiving. Turning the receive VFO then
cannot alter the stored TX home. No Master frequency write is attempted after
PTT, avoiding the TS-590 rejection which previously dropped the connection
and stranded a parked Sub. The original VFO state is restored when M ends.

The Sub stays on the listening frequency throughout Master TX unless PARKING
FREQUENCY is selected. Only the chosen TX protection action is applied to it.

Driver commissioning and updates
--------------------------------

Every .rmradio file has a driver revision in its header. RigMirror records
TEST DRIVER results locally against the exact SHA-256 bytes of that file:

  NOT YET TESTED
  TESTED SUCCESSFULLY ON THIS PC
  TEST FAILED

Changing or replacing even one byte makes it a new, untested driver. The
driver itself is never permanently stamped PASSED: all drivers can contain a
fault. Failed tests create a report containing the driver revision, hash and
CAT traffic. Future corrected drivers can therefore be released separately
from the Python application.

TEST DRIVER is optional. An untested or previously failed driver can still be
used to connect; CONNECT performs its own identity and frequency-read check.
TEST DRIVER is available when a fuller commissioning report is useful.

The 30 MHz limit is a RigMirror operating policy, not a radio-driver tuning
limit. A capable radio can connect and complete TEST DRIVER on 50 MHz; Mirror
simply pauses while the Master is above 30 MHz.

Polling
-------

NORMAL is 100 ms. FAST is 50 ms and may overload slower or older CAT
interfaces. Requests do not intentionally accumulate.

Bundled candidate drivers (25)
------------------------------

Kenwood:
  TS-990S
  TS-890S
  TS-590SG
  TS-590S
  TS-480SAT / TS-480HX
  TS-2000 / TS-2000X (HF operation only)

Icom:
  IC-7300
  IC-7300MK2 (ANT1 / RX ANT command included)
  IC-7610 (ANT1 / ANT2 command included)
  IC-705 (HF operation only)
  IC-7100 (HF operation only)
  IC-7850 / IC-7851
  IC-7700
  IC-7760
  IC-9100 (HF operation only)

Yaesu:
  FTDX101MP
  FTDX101D
  FTDX10
  FT-710 / FT-710 AESS
  FT-991A (HF operation only)
  FT-891
  FTDX5000 / D / MP
  FTDX3000
  FT-2000 / FT-2000D
  FT-950

Only documentation-derived or simulated confidence is claimed until each
exact driver revision passes on its real radio. Unsupported antenna routing
falls back to NO CHANGE or PARKING FREQUENCY.

Callum's first test
-------------------

1. Load and connect the bundled TS-990S and TS-590SG drivers. TEST DRIVER is
   optional, although useful if a connection or command behaves unexpectedly.
2. TS-990 Master / TS-590 Sub: test Mirror, M, Split and all three applicable
   TX protection choices.
3. Reverse the roles. With TS-590 Master, arm M at 14.300.000, tune to
   14.299.980 and transmit. Confirm the TS-590 actually transmits at 14.300.000.
   With parking selected, confirm the TS-990 parks and restores on RX.
4. Connect or test the TS-590 at 50.144.000, then use it as Master. Confirm
   connection succeeds but the Sub is not retuned, its
   readout is grey with a red dot, M/Split are disabled, and operation resumes
   automatically below 30 MHz.
5. Switch off only the Sub. Confirm only that connection fails and the Master
   connection remains available.
6. Then distribute the candidate drivers to owners of the other radios. Ask
   for the generated .rmreport whenever TEST DRIVER fails.

Automated verification covers the protocol engines and simulated radio
responses. It cannot establish that an undocumented model variation accepts
every command; real-radio commissioning remains essential.
