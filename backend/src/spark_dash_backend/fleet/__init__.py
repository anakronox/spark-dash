"""The fleet updater's engine: check every Spark, score it, update it (AL).

Moved here from the standalone `spark-fleet-updates` service in AL3a, module
for module, so that the dashboard's backend runs it in-process instead of
proxying to a second container. The reasons are in the roadmap's AL1 -- one
node list, one HTTP server, one image, one set of docs -- and the line this
crosses is written down in AL0: the backend now holds the SSH key and opens
sessions to each Spark, for this feature only, off unless configured, with
every guard the standalone service had carried across intact.

What is NOT here, deliberately: that service's own HTTP handler, TLS context
and web page. The dashboard is the server and the panel is the page.

The modules keep the shape and the style they were written in, because until
AL3g archives the origin repo the two copies must stay diffable against each
other. `pyproject.toml` records the lint exemption that buys, and its sunset.
"""
