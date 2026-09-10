# Spotify True Shuffle for Home Assistant

A Home Assistant custom integration that uses **SpotifyPlus** as its Spotify Web API bridge and maintains a real no-repeat shuffle cycle for a Spotify playlist.

## What it does

- Source playlist is read-only.
- Target playlist is configurable by Spotify URL / URI / ID; its name is **not hard-coded**.
- Every unique source track is played at most once per cycle.
- Artist gap and album gap reduce back-to-back repetition.
- Tracks count as played only after a configurable time/percentage threshold.
- By default, a track counts **only when Spotify reports the configured target playlist as the playback context**. Playing the same song from Search, an album, or another playlist does not consume it from the True Shuffle cycle.
- Source changes are detected periodically. New source tracks are added to the remaining cycle; removed source tracks are removed from future playback.
- State is stored persistently in Home Assistant and survives restarts.
- The target playlist is rebuilt only when it is not the active Spotify context, avoiding playlist edits under active playback where possible.

## Requirement

Install and configure SpotifyPlus first:

https://github.com/thlucas1/homeassistantcomponent_spotifyplus

## HACS installation

1. HACS → Integrations → Custom repositories.
2. Add `https://github.com/rolex86/ha-spotify-true-shuffle` as **Integration**.
3. Install **Spotify True Shuffle** and restart Home Assistant.
4. Settings → Devices & services → Add integration → **Spotify True Shuffle**.
5. Choose the SpotifyPlus media player entity and paste source + target Spotify playlist URLs/IDs.

## First run

After setup the integration reads the source playlist but does **not** overwrite the target automatically. Press **Start new cycle** once. This clears the configured target playlist and fills it with the generated True Shuffle order.

Spotify's own Shuffle / Smart Shuffle should remain **off** for this target playlist.

## Important safety rule

The integration never writes to the configured source playlist. Write operations are limited to the configured target playlist.

## Current status

Initial MVP (`0.1.0`) for testing. Test with a small playlist before using a large source playlist.
