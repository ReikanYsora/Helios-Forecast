# Changelog

All notable changes to Helios Forecast are documented here. The project follows a
date-based versioning scheme (`YEAR.MONTH.PATCH`).

---

## 2026.9.5

The release that stops taking the whole machine's statistics down with it. A
Home Assistant instance running this integration was losing entire hours of
long-term statistics, for every integration on it, not just this one. The cause
was here, it had been here since the first version, and it is fixed at the root
rather than worked around.

### Fixed: the integration no longer breaks Home Assistant's hourly statistics

Home Assistant knows two kinds of long-term statistics. A series named after an
entity belongs to the recorder, which compiles it from that entity's state every
hour on its own. A series named `domain:something` belongs to the integration
that declares it, and the recorder never touches it.

This integration archives nine series: the seven weather variables (Open-Meteo
only serves a rolling 60-day window, the archive keeps them for good) and the
predicted production for hours that have since happened (the past curve the card
draws behind today). All nine were written under entity ids, on entities that
also carried a `state_class`. So there were two writers on the same series: the
recorder compiling the entity, and this integration importing the same hour. When
they landed on the same hour, the duplicate violated the unique index, and Home
Assistant rolls that back by abandoning the **entire** hourly compile, not just
the offending row. Every other integration on the machine silently lost that hour
of history too.

It was not theoretical, and not rare. On the maintainer's own instance, two hours
of 2026-09-06 kept 14 series out of 494: the nine written here, plus five belonging
to another integration that already used its own ids. A healthy hour on the same
instance holds 419. Everything else on that machine, every hour, was gone.

The nine series are now written under this integration's own ids,
`helios_forecast:<entry_id>_<series>`, which the recorder never compiles. One
writer by construction, so the collision cannot happen rather than happening
rarely. The seven weather sensors lose their `state_class` accordingly: they show
the current hour, and their history is the archive, which no longer needs an
entity to hang from.

**Your history is moved, not dropped.** On the first start after the update, every
archived hour is copied onto the new id, and each of those hours is then read back
at the new id by its own start time. The old series is deleted only once every one
of them answers. The check is on the hours themselves and not on how many there
are: the new id is already being filled by the integration's own backfill by then,
so a count could be satisfied entirely by hours the move never wrote, and the
series deleted next would have been the only copy of the oldest of them. If
anything does not line up, nothing is deleted, it is written to the log, and the
next start tries again. It waits for Home Assistant to have finished starting and
runs in the background, so it never holds the start up: on a 20 864-hour archive
a Raspberry Pi 5 took about a minute, with everything else already running. And it
runs on every start rather than once, so an installation that skips a version, or
is restored from a backup taken before the update, is repaired all the same; once
there is nothing left to move it costs a single metadata read. The one thing not
carried over is the 5-minute short-term statistics, kept 10 days at most:
integration-owned series are hourly by design in Home Assistant. The full
long-term history is carried over whole.

Where to find them afterwards: in a statistics or energy-date card, the archive
appears under its readable name (Cloud cover, Global irradiance, Predicted power,
and so on) rather than under a `sensor.` entity. History graphs of the weather
sensors themselves now show the recorded states only, which is what those sensors
are for.

### Fixed: the migration converts what it moves, and checks the hours rather than counting them

Home Assistant stores an entity-bound statistic in the unit the entity displayed, so
an installation on the US customary system holds its temperature in Fahrenheit, its
wind in miles per hour and its snow depth in feet. The move now names the unit it
wants on every read, so those values are converted rather than relabelled, and a
series whose unit cannot be converted is left exactly where it is with an error in
the log. Naming the unit matters even when the stored one already matches: left
unasked, the recorder converts to whatever the live entity happens to display.

The check that runs before the old series is deleted is on the hours themselves, not
on how many there are. The destination is already populated by the integration's own
backfill by then, so a row count there could be satisfied entirely by hours the move
never wrote.

### Fixed: an hour the weather service publishes late no longer goes missing

Open-Meteo publishes a past hour with a delay. The weather archive moved its
high-water mark to the current hour as soon as anything at all had been written,
while rows are only written for hours that carry a value, so a refresh with an older
hour to write while the newest was still missing stepped over the gap and never came
back to it. Until this release that hole was invisible, because the recorder compiled
the same entities and the late hour arrived through the entity's own state. The mark
now stops short of a trailing window, so the most recent hours are offered again on
every refresh until they arrive.

### Fixed: the battery projection loses an hour of house load on the clock changes

The forecast curve is built on the local clock, so a local day always carries 96
quarter-hour points whether that day is 23, 24 or 25 real hours long, and the battery
projection charged each of them a quarter of an hour of real time. On the two
changeover days it integrated the house load over an hour too little in autumn and an
hour too much in spring: 0.7 kWh at a 700 W night load, about 7 points of state of
charge on a 9.8 kWh battery. A step now lasts the real time to the next point, and on
the spring day, where four quarter-hours name local times that never happen and land
on the instants of the four that follow, one point per real instant is kept.

### Fixed: a meter reset no longer teaches the learning that the sky went dark

When an energy meter is reset, replaced, or restored from an older backup, Home
Assistant writes one enormous negative hour into the recorder. The sky-residual map
already refused those hours; the analog library clamped them to zero instead, which
filed a bright hour as one where the sky gave nothing and dragged every later
prediction under similar sun and cloud down with it. Both refuse a negative hour now.

### Changed: two terms the physical model never had

Measured against 1875 forecasts from 57 real installations, the bare physical model
over-promised by about 40 %, systematically, everywhere. Two terms were missing from
it.

The first is everything between the plane of the array and the meter that no optical
model covers: soiling, mismatch, wiring, connections, availability, and the inverter's
own conversion. Fitted by leaving one installation out at a time, the fleet asks for a
factor of 0.827; PVWatts' own defaults, 14 % system losses on a 96 % inverter, give
0.825. That is the same number arrived at twice, so it ships as 0.825. On its own it
takes the bare model's mean absolute error from 109 to 88 W per kWp and removes the
bias entirely.

The second is that glass reflects more of the sunbeam the further the sun is from
square-on, which no transposition accounts for. After removing each installation's own
constant bias, the residual error follows a repeatable curve with sun elevation that is
the same on every roof, in every sky: about 0.63 below ten degrees rising to 1.25 above
forty. The standard incidence-angle modifier for a glazed module carries that shape and
takes the error to 84 W per kWp.

What this means for you depends on how long the integration has been learning. On an
installation with sixty days of production history the learned correction had already
absorbed most of it, so the published forecast barely moves; what changes is that it
now gets there from a model that is right, rather than from a correction compensating
for one that is not. On a fresh install, on one with no production sensor, and on every
sky the learning has not seen yet, the forecast is simply better from the first day.

### Changed: `power_now` reads the same curve the card draws

"Now" always falls inside a fifteen-minute step that began in the past, and that
elapsed stretch exists twice: raw, meaning what the forecast said at the time, and
clamped by what the site has actually been seen to produce, which is what the card
draws. The sensor read the raw one. On a shaded roof, where the physical model cannot
see the tree, the headline sensor and the card disagreed by the whole height of the
learned ceiling, for the same instant on the same screen. The sensors now read the
curve the card draws. On such an installation `sensor.helios_forecast_power_now` will
read lower than before; that is the value the integration actually stands behind.

### Changed: the learning's global fallback is weighted by energy

The sky-residual map keeps a correction per sky cell and a global one for the cells it
has not seen yet, which on a young installation is most of them. That global figure
counted every hour the same, so the many small hours around sunrise and sunset
outweighed the few that carry the day's production, and the fallback drifted towards
whatever the model does at low sun. It is now weighted by the energy of each hour, so
it says what the installation does over a day rather than over a list of hours.

### Fixed: the hourly refresh no longer asks the weather service on the hour

The archive of the elapsed hour is rebuilt by a refresh armed for the hour boundary, and it asked
at five seconds past. Weather models publish on the hour and so does every scheduled task there is,
so every installation asked in the least answerable second of the hour, all of them together, and
the log filled with "Open-Meteo returned no weather data" a few seconds later. It is not a quota:
twenty of these requests back to back answer without a single refusal. The refresh now runs seven
minutes past, which nothing about rebuilding a finished hour makes urgent.

### Fixed: the seventh day of the forecast, everywhere west of Greenwich

Open-Meteo answers whole UTC days while the forecast horizon is built on your local
midnights, so at a negative offset the tail of the last day ran past the final
weather sample the service had sent. The curve did not stop there: it held that last
sample and carried it forward, with the correct sun geometry on top, which looks
exactly like an ordinary forecast. In Los Angeles the seventh day ran eight hours on
one frozen weather hour and came out about a fifth too high, in the day sensor and in
the energy handed to the Energy dashboard for those hours.

The forecast now stops at the last hour the weather actually describes, and the
request reaches a day further so your local horizon is covered at any offset. If you
are in the Americas, expect `energy_day_7` to read lower than before. That is the
correct figure.

### Fixed: hours lost for good when Open-Meteo goes quiet

Open-Meteo answering nothing is normal rather than an error, so a refresh reuses the
last series it received and carries on. It also wrote the weather archive from it and
moved its high-water mark on the clock, which filed a forecast as the observed record
and, worse, carried the mark over hours nobody had measured. Past the six-hour retry
window those hours were gone for good, even after the service came back. The archive
now stops at the hour the series in hand was actually fetched, so a reused series
freezes the mark instead of advancing it, and the recovery refresh writes every hour
the outage covered.

### Fixed: the learning no longer takes the inverter's ceiling for a dark sky

The residual map compared your meter against a model that never saw the entry-level
inverter limit, only the per-array ones, which an ordinary single-inverter
installation does not have. An array oversized against its inverter therefore taught
the map that the sky is dimmer than it is around noon, and the map took that off
every cloudy hour that never clips: ten kilowatts of panels behind a six-kilowatt
inverter learned 0.88 where the truth was 1.0.

The same learning refused an impossibly negative hour but not an impossibly positive
one, and it averages, so a single phantom hour from a replaced meter dragged the
whole sixty-day correction to its ceiling. Both learners now drop an hour above what
the panels can physically deliver, on the same threshold the check-up warns you on.

### Fixed: what the reliability index was actually measuring

Three things, each of which could only push the number up.

Recent skill carries the largest weight and is the only term that measures accuracy.
It compared the archived past forecast against the production that archive had been
fitted to, so it came out near perfect whatever the model was really doing: an
installation whose physics is wrong by a factor of two read a skill of 0.99.
Tomorrow's predicted total is now written down before that day happens, and the skill
is measured against those records. It needs a couple of days to appear on a fresh
install.

A signal that could not be computed was shared out among the others, so the index
rose exactly when there was least ground to trust it, which is a northern winter or a
run of overcast. It now divides by the full weight and reads as a floor: this much
could be established.

And when the cross-model ensemble call failed, the disagreement between models was
filled with zero, which is what perfect agreement looks like. An unanswered hour now
carries no value and the signal drops out.

### Fixed: a battery below its reserve, and a panel cooler than the air

A battery sitting under its configured reserve, after an outage or a manual
discharge, was projected from the reserve rather than from the charge it actually
holds, so the whole chart started above the level your inverter shows and every hour
after it inherited energy that is not there.

The wind term was subtracted from the air temperature rather than from the heat the
sun puts into the panel, so an overcast windy hour put the cell several degrees below
ambient and the thermal model turned that into a production bonus, at the hours with
the least sun in them.

### Fixed: the check-up no longer calls a hybrid inverter a mistake

A battery inverter is sized for the house, not for the array, so three kilowatts of
panels behind a ten-kilowatt hybrid is an ordinary installation. It was reported as
an error every time you opened the page. The mistake that check exists to catch is a
value typed in watts, a factor of a thousand, so the threshold is now wide enough to
leave real installations alone.

### Fixed: clearing a field in the options actually clears it

Everything that reads the configuration merges the entry's stored data over its
options, and the options form only ever wrote the options. A setting typed when you
first installed the integration therefore lived in the data and could not be removed
through the interface: emptying the battery entity field left the old entity driving
the projection. The options form now writes the whole configuration back as the
entry's data, so an empty field means empty.

### Fixed: a refresh no longer undoes your decisions

The configuration checks are published before anything is fetched, so a wrong field
shows even when the weather service is down. That publish went through the same
routine as the final one, which retires whatever is not in the list it is given, so
every issue raised by the data checks was deleted and created again seconds later,
in the same refresh. Deleting a repair issue throws away its registry entry, and with
it your decision to ignore that one: forty-eight times a day on a thirty-minute
refresh. The early publish only adds now.

### Changed: Home Assistant 2025.11 is now the minimum

The archived series carry a unit class in their metadata, which the recorder only
stores from 2025.11. On anything older no integration-owned series could be created
at all: the archive wrote nothing while looking healthy, and the migration then waited
out its timeout on each of the nine series, every start. The manifest said 2025.1,
which was simply not true of this release.

### Changed: the predicted-production archive stores the hour, not its first instant

The archived past forecast filed the modelled power at the top of each hour as that
hour's average. It is now the average across the hour, taken between the sample that
opens it and the one that opens the next. Morning hours were understated and
afternoon hours overstated, most of all around sunrise and sunset where the curve
moves fastest.

### Changed: the benchmark asks for no key at all

An earlier build stored a benchmark write key in the config entry. Nothing reads it
any more: taking part is a box in the settings and nothing else, and the entry is
cleaned of the key on the next start. Diagnostics now masks anything whose name reads
like a credential, since that file is what this project invites you to attach to a
public issue.

The measurement itself starts over with this release and only accepts 2026.9.5 or
newer, so the published averages describe the model rather than the gaps between
versions. An installation that had already opted in stays opted in. What goes up is
the predicted curve, the geometry of the installation and the production your meter
measured, once an hour, under a hash of the config entry: no entity name, no
consumption, no account, coordinates rounded to about a kilometre before they leave
your machine. Results at helios-ha.org/benchmark.

### Fixed: Korea gets the Korean weather model

The regional model boxes overlap, and Korea's sits entirely inside Japan's. The rule
that picked between them preferred whichever box the point sat most centrally inside,
which near the small box's own edge is the vast one around it: Jeju island was
forecast from the Japanese model. A box that wholly contains another candidate is now
set aside in favour of the narrower one.

### Removed: the `predicted_power` and `predicted_energy` entities

Both existed for one reason: to give the predicted-production archive an entity to
be named after. That is exactly what no longer happens, and their live value was a
duplicate of `power_now` and `energy_this_hour` respectively. Rather than keep two
entities that do nothing and carry a deprecation for a year, they are removed now.

- `sensor.helios_forecast_predicted_power` is replaced by
  **`sensor.helios_forecast_power_now`** (same value, same unit, same device class).
- `sensor.helios_forecast_predicted_energy` is replaced by
  **`sensor.helios_forecast_energy_this_hour`** (same value, same unit; it is
  disabled by default, enable it from the integration's entity list).

A dashboard or automation naming either of them needs that one edit. Their archived
history is not lost: it is moved to `helios_forecast:<entry_id>_predicted_power` and
`helios_forecast:<entry_id>_predicted_energy` like everything else, and their now
empty registry entries are removed so no dead entity is left behind.

---

## 2026.9.4

### Fixed: saving the settings no longer drops the benchmark opt-in and key

Since the benchmark got a step of its own in 2026.9.2, the settings step rewrote
the entry with only the fields its own form shows, and the benchmark block was not
among them: saving the installation settings switched the benchmark off and blanked
the key. 2026.9.3 made it visible, because a repair is exactly what sends people to
that form. Each step now rewrites only the keys it shows and carries the others
over. If it happened to you, switching the benchmark back on with the same key, or
a fresh one, puts you right where you were: the collector follows the installation,
not the key, so nothing measured is lost. Clearing the key on the benchmark step now
really clears it, as the documentation always said.

---

## 2026.9.3

The release that checks its own configuration. The first two days of the public
benchmark showed one installation in seven running on a peak power typed in watts,
and a forecast on a wrong configuration is wrong with a straight face: every field,
entity and data source is now verified and each problem becomes a repair the owner
sees. The same two days located a systematic under-forecast on clear mornings and
its cause in the analog ensemble, which now reads ratios to the physics rather than
watts; replayed on thirty days, the error on the day's energy falls by a third. Plus
the consumption profile that a sparse source no longer dilutes, and a diagnostics
download.

### Added: a systematic check-up of the configuration, shown as repairs

A forecast running on a wrong configuration produces wrong numbers with a straight
face. The first two days of the public benchmark showed how common that is: one
installation in seven had typed its peak power in watts instead of kilowatts, one
had an inverter limit in watts, one a meter that counts the whole house. None of
it crashes, all of it makes the forecast look bad for a reason that is not the
forecast.

So every field is now checked, at startup and after every refresh, and each
problem found becomes a repair in Home Assistant (Settings, then Repairs, and the
integration's own page), naming the value at fault and what to do about it. The
configuration first: peak power, tilt, azimuth and tracker of each line, its
coordinates against the installation's, the inverter limits against the panels,
the location against the Home Assistant home, the battery block, the trend hour,
the benchmark key. Then the entities it names: the production sensor must exist
and be a cumulative energy sensor, not a power one; the battery sensor must exist
and report a percentage; the curtailment signal must exist. Then the data itself:
a production sensor with no history yet, one that has not moved for days, one that
records energy in the middle of the night (it measures more than the panels), one
that exceeds what the declared panels can deliver. A repair clears itself the
moment the configuration is corrected.

The community benchmark answers each upload with its verdict on the installation,
and an installation it keeps out of the published figures now learns why from a
repair of its own instead of from the website.

### Changed: the analog ensemble reads ratios, not watts

The second stage of the learning looks up past hours whose sun position and cloud
cover resemble the hour being forecast, and until now took the median of what the
installation produced in those hours, in watts. The public benchmark's first two
days showed the flaw across the fleet: on clear mornings the forecast ran half
below the meter, because the nearest analogs of a September morning, at the same
sun altitude, are July hours at a more northerly azimuth where a south roof made
less. The ensemble now stores each analog as the ratio of what the site produced
to what the physical model said for that same hour, and applies the median ratio
to today's physics: the geometry difference is the physics' business, the analog
only says how the site departs from it (shading, soiling, an orientation a few
degrees off). The learned ceiling and the P10/P90 band follow the same rule.

Replayed on thirty days of one installation with the archived weather, the hourly
error falls from 53 to 46 W per kWp, the error on the day's energy from 11.1 % to
7.3 %, and the band still holds 91 % of the hours for a target of 80. A library
without a physical model for its hours (no layout yet) keeps reading watts.

### Fixed: a consumption source with gaps no longer dilutes the learned profile

The home consumption profile behind the battery projection summed every Energy
dashboard source hour by hour, and an hour with no bucket for one source still
counted, with that term missing. A battery whose discharge meter reported a
quarter of the time carried the night load a quarter of the time, so the profile
learned a house that barely consumes after dark and the projection never reached
the reserve (reported in #61). The recorder writes an hourly row as soon as a
sensor had a valid state in that hour, so a missing row means no data, not zero:
the profile is now built only from the hours every sparse source covers, the
per-source coverage is exposed on the predicted state of charge sensor and in the
diagnostics, and a source far behind the others gets a repair naming it.

### Added: diagnostics

The integration's page now offers a diagnostics download: the configuration with
its benchmark key blanked, the problem list, how much history the learning stands
on and the consumption coverage. Enough for an issue, nothing that names a person
or an address.

---

## 2026.9.2

### Added: contribute to the public accuracy benchmark

A forecast can only be judged against what actually happened, and no provider
serves its own past emissions: a prediction nobody wrote down at the moment it
was made cannot be recovered later. So "accurate" stays an adjective until
somebody starts recording. This release adds the recorder, and a menu of its own
to join it: Configure, then the community benchmark, with a link to the page.

Switched on there, an installation posts once an hour the curve it is currently
predicting, together with the production it has already measured. A collector
scores the two against each other once the day is over, alongside the same
measurement taken from other forecast providers, and the results are published
openly at helios-ha.org/benchmark.

It is off unless you turn it on, and it needs a key, which you get in one click
with no account and no name. What it sends is fixed and small: your panel
geometry, the predicted curve with the cloud cover behind it, the measured
production and the reliability index. No entity names, no consumption, no other
sensor, and your coordinates rounded to about a kilometre, which no weather model
can tell apart from the exact spot. The installation is identified by a hash, so
one site can be followed over time without the collector ever being told whose it
is. The upload runs beside the forecast and never inside it: a collector that is
slow, unreachable or gone cannot delay or break anything. Clear the key and
everything stops within the second.


### Fixed: the type check on the curtailment cap

The inverter cap was validated through a local flag the type checker could not
follow, so the published tree failed its own type gate. Same behaviour, narrowed
where it is read.

---

## 2026.9.1

A corrective release on top of 2026.9.0, plus the battery projection's extremes as
entities, a learning that leaves curtailed hours out, and one addition for the card.

### Changed: the learning no longer counts your inverter's limits twice

The learned sky correction kept every produced hour, curtailed ones included, on
the reasoning that curtailment is part of what the home really harvests. It is,
but the cap was then counted twice: once burned into the learned ratio (a run of
full-battery afternoons pulled the ratio for those sun positions well below one),
and once more at forecast time, where the inverter cap is already applied. The
depressed ratio then hit the days nothing was clipped, an empty battery after a
cloudy day, and the forecast landed below the cap for the whole afternoon. A
curtailed hour is now treated for what it is, a lower bound: it is left out of the
sky-residual map when it sits under the model (when it reaches the model it counts
as before) and out of the analog library altogether. The battery case needs nothing
new, a full battery (state of charge sensor) at the entry-level inverter cap is
enough; for
zero-export and grid-limited installs a new optional **curtailment signal**
(binary sensor, input boolean or switch, on while the inverter is held back)
marks the hours. Thanks to @Manama2011 for the analysis and the proposal, and to
@Legotechniker for the case that started it (#46).

### Added: the battery projection's low and high points as entities

`battery_min_soc`, `battery_min_soc_time`, `battery_max_soc` and `battery_max_soc_time`
join the predicted battery state of charge as entities of their own (disabled by
default, like the day peaks), so a tile can show when the battery bottoms out and an
automation can trigger on the projected low without a template sensor in between. The
attributes on the SoC sensor stay as they were. Thanks to @Manama2011 for the
proposal (#57).

### Fixed: a nameplate-high plateau right before "now" on the card's forecast curve

Third round of #52. The card's series switched from the hourly archive to the live
points at the archive's last point, but that point stands for its whole hour, so the
sub-hourly live points inside that hour, plus the stretch up to "now", came through raw:
the live series deliberately leaves its already-elapsed points unclamped (what the
forecast said at the time), and next to the archive's clamped hours they drew a plateau
at the panels' nameplate for up to an hour before "now". The series now takes the
archive through the end of its last hour, then a clamped copy of the elapsed stretch,
then the live points from "now" on. Thanks to @ruteclrp for the screenshots that showed
exactly where the plateau sat (#52).

### Added: the card can read where your arrays are

A new `helios_forecast/layout` websocket command hands the Helios card the lines of
a config entry: each line's azimuth, tilt, tracker kind, share and, when the line
carries its own coordinates, where it stands. The card (from 2026.9.4) uses it to
mark every array in the scene and point its own ray at the sun. Geometry only,
nothing about production, and nothing changes for an install without the card.

### Fixed: `power_now_low` / `power_now_high` stayed unknown forever

These two sensors interpolate the analog P10/P90 band around the current instant, but
the bucket immediately before "now" never carries a band (past points are left as the
plain physical-model output, by design), so the interpolation always had a missing
side and gave up. Every install hit this from the first refresh, though it only became
visible once the analog library had enough history for the band to actually exist on
the future side. Both sensors now fall back to whichever side of "now" does have a
band. Thanks to @Manama2011 for the detailed report and root-cause diagnosis (#51,
filed as Helios#421).

### Fixed: "no battery configured" logged as a WARNING on every restart

A PV-only install with no battery would log "Helios battery SoC projection is off:
no battery is configured..." as a WARNING on every restart, even though there is
nothing to act on: the projection simply doesn't apply without a battery. It now
logs at INFO instead. Thanks to @huma-meng for pointing it out (#50).

### Fixed: battery SoC projection off for a full cycle after every restart

The listener that re-projects the SoC the moment its source entity comes back was
registered after the coordinator's first refresh, so it always missed the entity's
own first appearance at startup: the projection stayed `unknown` for a full 30-minute
cycle before recovering on its own. The listener now arms before that first refresh
runs. Thanks to @Manama2011 for the detailed report and root-cause diagnosis (#53).

### Improved: `power_now_low` / `power_now_high` report 0 W at night instead of unknown

Night points carried no P10/P90 band at all (nothing to draw an analog ensemble from
with the sun below the horizon), so both sensors went `unknown` from dusk to dawn every
day, punching a nightly hole into their history and long-term statistics. The output
there isn't uncertain though, it's known exactly: 0 W, and so are its 10th and 90th
percentiles. Night points now carry that zero band, keeping both sensors continuous
across the night. Thanks to @Manama2011 for the report, root-cause diagnosis and patch
(#54).

### Fixed: `energy_today_remaining` went `unknown` for the last 15-30 minutes of every day

Once the day's last forecast bucket had passed (23:45 local, at the default 15-minute
step), the `[now, midnight)` window used to compute the sensor held no bucket at all, so
it published `unknown` instead of the honest answer: 0 kWh left today. `energy_this_hour`
/ `energy_next_hour` never showed this since their hour-aligned windows still contain a
bucket. An empty window now reports 0.0 kWh whenever it still falls inside the forecast
horizon, and only stays `unknown` for a genuine gap outside it. Thanks to @Manama2011 for
the report, root-cause diagnosis and patch (#55).

### Fixed: the past-forecast archive could hug the panels' nameplate ceiling on a clear day

The hourly archive that backs HA's long-term statistics and the card's past-forecast curve
was residual-corrected, but never analog-enriched: unlike the live forecast's future points,
it never got the ceiling that reins the physical model back down to what the site has
actually produced under similar sun and cloud conditions. A well-learned install (weeks of
history, properly sized inverter) could still see its archived curve flatten at the array's
DC nameplate for hours around midday, right where the live forecast next to it, which does
get the clamp, looked accurate. The archive now goes through the same analog enrichment as
the live forecast. Thanks to @ruteclrp for the report and for ruling out the simpler
explanations (#52).

### Fixed: today's own elapsed hours still weren't corrected on the card's past curve

Superseded by the plateau fix above, which moves the split to the end of the
archive's last hour. The previous fix corrected the archive itself, but the card reads its past-forecast curve
through a separate websocket command that was splitting archive vs. live at today's
midnight, not at the archive's own last point. Today's already-elapsed hours came from the
live series instead, which deliberately leaves past points unclamped (a past point there
means "what the forecast said at the time"), so they kept the same nameplate-hugging
behaviour the first half of this fix was meant to remove. The split now happens at the
archive's own last point regardless of the calendar day, so today's elapsed hours get the
same analog-clamped values yesterday's already did. Thanks to @ruteclrp again for confirming
the first fix only got halfway there (#52).

### Fixed: the just-elapsed hour could stay unclamped for up to 30 minutes

The archive rebuilds at most once an hour by design, a deliberate CPU saving, but that
check only ran inside the regular 30-minute refresh, not right when the hour actually
rolled over. Depending on where in that 30-minute cycle the boundary fell, the hour that
had just finished could sit unclamped (hugging the nameplate ceiling again) for up to half
an hour before the next refresh caught up, which is exactly what made the previous fix look
like it was working, then not, then working again. A dedicated hourly trigger now forces
the refresh the moment the hour changes, instead of waiting on the next tick. Thanks to
@ruteclrp for the patience through three rounds of this (#52).

---

## 2026.9.0

A release about putting the forecast to work: the full curve now reaches your
automations, a battery can be projected forward, and the forecast now reads the
sky exactly like the Helios card.

### Added: the full forecast, in your automations

The integration already computes the whole production curve, not just the current
value. It's now available to automations two ways: a **`helios_forecast.get_forecast`
service** that returns the remaining curve on demand (15-minute points, each with
the predicted watts and the P10/P90 confidence band), and the same curve as a
`forecast` attribute on the `power_now` sensor. An energy management system can
read it to shift battery charging into the expected peak, work out how much sun is
still to come, or find the latest safe charging start. Thanks to the detailed
write-up on #35.

### Added: a predicted battery state of charge

If you have a battery, Helios Forecast can now project its **state of charge over
the next 48 hours**. It runs the production forecast against your home's own
consumption, derived straight from your **Home Assistant Energy dashboard**, so
there's no extra sensor to wire, and integrates the battery's charge from your
current level, within your capacity, reserve and charge/discharge limits. Turn it
on by filling in your battery capacity and state-of-charge sensor in the
integration settings; a **Predicted battery state of charge** sensor then appears,
carrying the full curve, the day's projected low and high, and the forecast
reliability. There's also a `helios_forecast.get_battery_soc_forecast` service.

It predicts, it never commands: sending the charge order stays with your own
automation, which knows your inverter. And because home consumption is a learned
average, it's an honest steer read alongside the reliability figure, not a
guarantee. Thanks to @brunnwart and @jasonyates for the design brief (#25).

### Changed: the forecast transposes the sun itself, no GTI dependency

Helios Forecast no longer pulls a global tilted-irradiance (plane-of-array)
supply; it transposes the horizontal irradiance onto each panel plane itself,
exactly like the Helios card does. One fewer external dependency, and the two
stay in lockstep. It also removes a flip-flop where some refreshes used
Open-Meteo's own tilted value and others a fallback, giving different
magnitudes from one update to the next.

### Changed: the weather request now matches the Helios card

The Open-Meteo request mirrors the card's: the same model picker (a regional
high-resolution model paired with a global one, median-fused), the same weighted
cloud-cover layers, and instant irradiance. Card and forecast read the same sky.

### Fixed: the learned correction no longer confuses inverter clipping with weather

If a panel line has its own inverter cap, the correction the forecast learns from
your production history used to be trained against the *uncapped* theoretical
output, so an afternoon where the cap genuinely limited output read as an
underperforming sky and pulled the learned ratio down for that reason alone. The
learning now sees the same capped output the forecast itself produces, so the
correction reflects the weather, not your hardware ceiling. Only affects
installations with a per-line inverter cap configured.

### Fixed: a gap in temperature history no longer inflates forecast confidence

The forecast picks its closest historical matches partly on outdoor temperature,
and a match missing that reading used to count as a perfect one, tying with, or
even beating, a match with a real but tiny temperature difference. If your
temperature source has gaps (added partway through the learning window, or
occasional dropouts), those gaps no longer masquerade as ideal matches: reported
confidence and the learned production ceiling now reflect what the history
actually supports.

### Fixed: ready for a future Home Assistant statistics change

A coming Home Assistant version tightens what the long-term statistics import
expects; the integration now declares the new fields up front, so the archived
weather and predicted-production history keep importing cleanly across the change.
No user action needed. Thanks to @FoxP for pointing out the breakage radar (#38).

### Fixed: the battery SoC projection could crash, and now says why it skips

The state-of-charge projection could error out, and when it correctly declined
to run because an input was missing it did so silently, so an empty
`get_battery_soc_forecast` gave no clue why. It no longer crashes, and it logs a
clear reason whenever the projection is off, so you can tell at a glance what to
fill in. Thanks to @FoxP (#40).

### Fixed: the SoC projection recovers the moment the battery sensor is back

On startup a battery integration can leave its state-of-charge sensor unavailable
for a few seconds while it connects (a modbus link, for instance). The projection
read the sensor at that instant, found it unavailable and stayed off until the
next 30-minute refresh. It now re-projects as soon as the sensor becomes available
again, and a brief startup gap is logged gently rather than as a warning. Thanks
to @FoxP (#42).

### Fixed: border locations pick the right regional weather model

The forecast pairs a global weather model with the best regional high-resolution
one for your location, using the same picker as the Helios card. The regional
coverage areas overlap at national borders, and the first area listed used to
win, so a site near a border, or inside a smaller area enclosed by a larger one,
could be read with a neighbour's model. It now picks the area your location sits
most centrally within, so the regional model matches where you actually are.
Thanks to @MatCos.

### Fixed: wind speed was read in the wrong unit, running the forecast hot

Open-Meteo returns wind at 10 m in km/h, but the cell-temperature model that
derates the forecast for a hot panel expects m/s. Reading the km/h figure as
if it were m/s made the modelled cell run too cool, which overstated the
forecast: about 7% high at a moderate 5 m/s wind, and 14% at 10 m/s. Converted
at the single point wind enters the model; your wind sensor and its history
are untouched, still in km/h. Thanks to @Happyfield7 (#44, #45).

### Added: a coordinate override per panel line

A panel line normally inherits the entry's home coordinates, which is right for
one roof but not for a line mounted somewhere else entirely, like a garage or a
carport. Each line can now set its own latitude and longitude, left blank to
keep inheriting the shared location.

### Fixed: a decimal latitude or longitude could get misread

Latitude and longitude used the same plain numeric field every other decimal
value in this integration had already moved away from, for the same reason:
a browser's own locale can misparse a typed decimal. They now use the same
numeric input as every other geometry and power field.

### Fixed: the detail series honours the resolution and pre-correction total it already documented

The `helios_forecast/series` websocket command has always documented an optional
`resolution_min` parameter and a pre-correction `kwh_raw` daily figure, but
sending the former was rejected outright, and the latter silently mirrored the
corrected total instead of a genuine one. Both now do what they always said they
did: an explicit resolution resamples the curve server-side, and `kwh_raw` is a
real daily total summed from the uncorrected physical model. Omitting the
resolution parameter, which is what the Helios card does today, is unaffected.

### Fixed: a broken Open-Meteo reply no longer skips the retry it was meant to use

A reply that came back with the right HTTP status but a malformed or unexpected
body (a proxy hiccup, a truncated response) used to escape the retry logic
entirely and fail the whole refresh outright, instead of being treated as the
transient blip the retry mechanism already exists to absorb.

### Fixed: a weather sensor with no display details no longer takes the rest down with it

A single weather field missing its display metadata used to abort the entire
sensor platform setup, taking down power, energy, reliability and every other
entity along with the one weather sensor actually at fault. It's now skipped on
its own; every other entity sets up normally.

---

## 2026.8.3

A performance and reliability release on top of 2026.8.2.

### Fixed: high CPU and network stalls every 30 minutes

Since 2026.8.2, each 30-minute refresh ran its whole forecast computation on Home Assistant's event
loop and rebuilt the full 60-day predicted-production archive every time, which briefly pinned a CPU
core to 100% and could stall the network for a few seconds on small systems. The refresh now runs its
heavy work off the event loop, rebuilds the 60-day archive at most once an hour, memoises the
sun-position maths, caps the concurrent Open-Meteo requests, writes the weather statistics
incrementally instead of re-importing 60 days every time, and precomputes each refresh's time axes
once instead of rebuilding them on every sample. Thanks to the users who reported it with detailed
CPU traces.

### Fixed: forecast sensors could stay unavailable until a manual update

A stalled network request had no timeout, so if a fetch hung, that config entry's refresh could
freeze and leave its sensors unavailable for a long time until `homeassistant.update_entity` was
called by hand (#32). Requests now time out and fall back to the retry and last-good-value path, so a
temporary network problem recovers on its own at the next cycle. Thanks to @CaneTLOTW for the
detailed report.

---

## 2026.8.2

A corrective release on top of 2026.8.1.

### Fixed

- **Faster startup.** The integration fetched each panel orientation's irradiance from
  Open-Meteo one after another and ran the full 60-day statistics backfill during setup, so a
  multi-orientation install was slow to appear. It now fetches every orientation in parallel
  and moves the archiving to a background task, so setup finishes promptly. (#31) Thanks to
  @FoxP.

- **Over-prediction curbed on shaded sites.** The physical model cannot see near-field shadows
  (a tree in the morning, a roof in the evening), so at low learning confidence the forecast
  could predict well above what a site actually produces. Once enough close analogs exist, the
  forecast is now capped at the site's own observed production under similar sun and cloud (with
  a margin), which reins in the over-prediction while still allowing an unusually clear day.
  (#28) Thanks to @ManuMaxGit and @ferreto1978.

### Changed

- **A leaner default entity set.** A fresh install added a large entity set to the recorder.
  Only the everyday values are now enabled by default (power now, energy today remaining, today
  and tomorrow energy, and reliability); the rest are registered but disabled, so you switch on
  only what you automate on, and enabling one later never loses its history. (#30) Thanks to
  @rapahl.

### Packaging

- Installable from the **HACS default store**, with an integration icon shown on the HACS
  repository line, and a refreshed README (absolute images so they render in HACS, restyled
  badges).

---

## 2026.8.1

### Added

- **A per-line inverter limit.** Each panel line can now carry its own optional inverter
  cap, on top of the entry-level one. When set, the line is clipped at its own ceiling
  before the lines are summed, so a micro-inverter string that saturates on its own is
  modelled correctly instead of being bounded only on the combined total. Left empty, a
  line is uncapped and behaves exactly as before. (#26)

### Fixed

- **Cloud cover now matches the Open-Meteo app.** The weather inputs were blended across
  several forecast models and reduced to their median, which could outvote the one model
  that is actually right for a location and, for instance, show a fully overcast tomorrow
  as clear. The values now come from best_match, the same single model the Open-Meteo app
  displays and the one the tilted-irradiance request already used, so the cloud curve lines
  up with what you see in the app. The cross-model spread is still read as an uncertainty
  signal for the reliability index. (#22)
- **The forecast no longer reads sunnier than the Helios card.** Because of that same model
  blend, the forecast could sit a little above the card's own figure and over-estimate
  production. With the inputs back on best_match, the integration and the card read the same
  source and track much more closely. (#27)

### Changed

- Refreshed the integration branding (icon and logo).

---

## 2026.8.0

### Added

- **Several panel lines in one entry.** A single entry can now hold more than one panel
  line, added through an "add another line" step. This is the setup for two strings on one
  inverter that only exposes a single, combined production sensor: the lines share that
  sensor and one inverter limit, and the model sums them by kWp share. One entry per line
  is still fully supported for installs that do have a sensor per line. The Configure
  button opens a menu to edit the shared settings or the lines (edit, add or remove).
  (#18)
- **`forecast` attribute on the weather sensors.** Every archived Open-Meteo sensor -
  cloud cover, irradiance, temperature, wind, snow, now carries a forward-looking hourly
  `forecast` list (today plus the 7-day horizon), the same shape the power sensor exposes,
  so you can plot the upcoming sky next to the predicted power in ApexCharts. (#21)

### Fixed

- **Decimal peak power and inverter limits are entered reliably.** The peak power, tilt,
  azimuth and inverter fields are now proper numeric inputs with a defined decimal step,
  so a value like `2.61 kWp` is stored at full precision instead of being misread by the
  browser's number locale. (#13)
- **A transient Open-Meteo blank no longer errors.** An empty or rate-limited weather
  response is now retried a few times, and if it still comes back empty the last good
  fetch is reused for that cycle rather than failing the update with "Open-Meteo returned
  no weather data". (#19)
- **The production sensor picker only offers energy sensors now.** The learned correction
  reads the sensor's long-term sum statistics (hourly kWh change); a power sensor (W) has
  none, which silently disabled the learning and capped the reliability index at ~36 %.
  The picker is filtered to energy sensors, the field explains what to pick (a cumulative
  kWh sensor, e.g. the inverter's total production), and installs that already point at a
  sensor without usable statistics now log an explicit warning instead of failing
  silently.

---

## 2026.7.2

### Fixed

- **Setup no longer crashes on a missing cloud hour.** Open-Meteo can leave an hour with
  no cloud value (`None`); the sky-residual code then raised `TypeError`, failing the
  coordinator update in a loop (`setup_retry`) whenever a production sensor was
  configured. A missing hour is now treated as clear sky. (#14)

### Improved

- **Reliability degrades gently across the horizon.** The per-day reliability used a steep
  12 %/day linear decay that bottomed out at its floor by day +5. It now decays
  exponentially toward a higher floor (day 0 = 100 %, day +3 ~71 %, day +6 ~58 %), which
  matches how weather-model skill actually degrades, so the later days are no longer
  under-rated. (#16)
- **The learned correction now accounts for outdoor temperature.** Panels lose output as
  they heat up (about 0.35 %/degC of cell temperature). The analog ensemble now matches
  past hours on temperature as well as cloud cover and sun geometry, so a hot day and a
  cool day with the same sky are no longer averaged together. (#17)

### Internal

- Full strict cleanup pass: ruff lint and formatting, a fully type-checked package (mypy),
  and a CI lint job so it stays clean. No behaviour change.

---

## 2026.7.1

First HACS release of Helios Forecast: a native solar-production forecast provider for the
Home Assistant Energy dashboard, a clean set of sensors (power now and next hour, per-day
energy and peaks over a 7-day horizon, energy left today), and a sub-hourly detail series
the Helios card reads. Configuration is one entry per panel line, so each roof orientation
gets its own forecast, device and entities.
