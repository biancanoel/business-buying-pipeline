# Census location data

Rows for California, Oregon and Nevada from the US Census Bureau's 2024 Gazetteer files
(public domain), used to place listings on a map:

- `census_2024_place_ca_or_nv.txt`: cities, towns and CDPs, from `2024_Gaz_place_national.zip`
- `census_2024_counties_ca_or_nv.txt`: counties, from `2024_Gaz_counties_national.zip`

Source: https://www.census.gov/geographies/reference-files/time-series/geo/gazetteer-files.html

The files keep the Census format (tab-separated, original columns); only rows for other
states were removed and trailing spaces trimmed. To add a state, filter the national files again.
