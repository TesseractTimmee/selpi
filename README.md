# Selpi

Selpi is woefully incomplete, but in the long run it will hopefully be a collection of basic utilties to monitor a Selectronic SP Pro 2 from a RaspberryPi.

Additional docs:

 * [Connecting](docs/connecting.md)
 * [Protocol](docs/protocol.md)
 * [Developing](docs/developing.md)
 * [Grafana / InfluxDB migration](docs/grafana-migration.md) (replacing the old Select.Live emulation)

# License / Warranty

See the [LICENSE](LICENSE.md) file for license rights and limitations (MIT).

For those unfamiliar please note that this means no warranty OF ANY KIND is granted with this code, further it is possible using this code may impact on any warranty from Selectronic.

# Getting Started

Ensure your device is [connecting](docs/connecting.md) to the SP Pro.

[Pipenv](https://github.com/pypa/pipenv) is used to control managed dependencies, if it's not already installed, install it:

```
$ pip install pipenv
[...]
Successfully installed certifi-2019.9.11 enum34-1.1.6 pip-19.3.1 pipenv-2018.11.26 setuptools-41.4.0 typing-3.7.4.1 virtualenv-16.7.6 virtualenv-clone-0.5.3
```

Install dependencies with:

```
$ pipenv install
```

The `./selpi` will try to launch `selpi` within the pipenv, so just run:

```
$ ./selpi
Loading .env environment variables...
usage: selpi.py [-h] [--log {info,debug,warning,error,critical}] {command} ...

positional arguments:
  {command}             command to run
    dump                dump memory to stdout
    proxy               expose SP PRO over TCP proxy
    scan                scan known addresses

options:
  -h, --help            show this help message and exit
  --log {info,debug,warning,error,critical}
                        log level
```

# Commands

 * `selpi live [--screen now,today,tech,dc,ac,...|all] [--json] [--list]`: every value SP LINK's data screens show (288 fields, see docs/protocol.md "Live data").


Selpi uses a sub command structure meaning the base `selpi` accepts multiple commands that run it in different modes.

See `selpi --help` for a full list of commands, some commands are documented below.

Additional logging output can be printed with `selpi --log=debug ...`.

## proxy

The `proxy` command is used to expose the Selectronic SP PRO over TCP.

The listening address and port can be controlled from `.env.local`.

To start the proxy run:

```
$ ./selpi proxy
```

TP LINK can then be configured to connect to the device `selpi` is started on.

# Acknowledgements

This repository is primarily maintained by [David Schoen](http://github.com/neerolyte).

Foundational code and inspirational advice provided by [Justin Stafford](https://www.linkedin.com/in/justin-stafford-blueshift/).

## Configuration backup and data logs

Built from the protocol decoded in [docs/protocol.md](docs/protocol.md) (SP PRO firmware 15.47, SP LINK 15.40).

```
selpi config show InitialReturnVoltage        # named settings with address and raw value
selpi config backup --out backups/            # Config_<time>.SPLC + ServiceSettings_<time>.CSV (SP LINK format)
selpi config backup --json --out backups/     # names, addresses and values as JSON
selpi logs --since 2026-11-14 --out logs/     # EventData / DetailedData / DailySummaryData CSVs (SP LINK format)
```

`config` only reads. `logs` writes download mode (`0xa251`) and each log's range register, nothing else.
Requests are retried on a new connection, because Select.Live occasionally drops a reply.
