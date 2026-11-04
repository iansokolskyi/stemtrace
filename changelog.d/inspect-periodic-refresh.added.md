- While a worker is live, the server asks the workers every half of `anomaly_registry_window` (5
  minutes by default), so an idle fleet stays trusted and a `refresh=false` request still reports
  unregistered tasks. The background ask uses the same 2-second interval and failure wait as page
  loads, and doesn't run while no worker is live or without a broker URL.
