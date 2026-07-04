// Mapbox public access token (pk.*), single source of truth for every
// dashboard in this repo. Required for the satellite-streets basemap; paste
// yours here. Mapbox's free tier (50k web map loads + 200k raster-tile
// requests/month) is well above what a research dashboard needs. Without a
// valid token the satellite toggle silently falls back to a blank layer and
// the dashboard still works on the Carto basemap.
//
// Both the trip/route dashboards (this folder) and the observation_tool
// comparison dashboard import this module, so the token lives in exactly one
// place until those dashboards are merged.
export const MAPBOX_TOKEN =
  "pk.eyJ1IjoiYWFnYSIsImEiOiJjazVlYWZtdXMwZG80M21tNHR2a2p4bHpmIn0.xWIYS40OSkd1A6dQw0kIDg";
