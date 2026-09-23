# BIOMERO.importer - Automated Data Import System
[![Build BIOMERO.importer Docker Image](https://github.com/NL-BioImaging/BIOMERO.importer/actions/workflows/docker-image.yml/badge.svg)](https://github.com/NL-BioImaging/BIOMERO.importer/actions/workflows/docker-image.yml) [![Publish BIOMERO.importer to PyPI](https://github.com/NL-BioImaging/BIOMERO.importer/actions/workflows/publish-to-pypi.yml/badge.svg)](https://github.com/NL-BioImaging/BIOMERO.importer/actions/workflows/publish-to-pypi.yml) [![Python package](https://github.com/NL-BioImaging/BIOMERO.importer/actions/workflows/python-package.yml/badge.svg)](https://github.com/NL-BioImaging/BIOMERO.importer/actions/workflows/python-package.yml)
> 🚀 **This package is part of <img src="https://raw.githubusercontent.com/NL-BioImaging/OMERO.biomero/refs/tags/v1.2.1/webapp/src/img/biomero-logo.svg" alt="BIOMERO" height="16" style="height:16px; width:auto; vertical-align:middle;"> BIOMERO 2.0** — For complete deployment and FAIR infrastructure setup, start with the [**NL-BIOMERO Documentation**](https://nl-bioimaging.github.io/NL-BIOMERO/) 📖

The BIOMERO.importer system enables automated uploading of image data from microscope workstations to an OMERO server. BIOMERO.importer is a database-driven system that polls a PostgreSQL database for new import orders and processes them automatically, including the option of running preprocessing containers for e.g. file conversion or pyramid creation.

## Documentation

- [Importer documentation](https://nl-bioimaging.github.io/BIOMERO.importer/): configuration, storage, import orders and development.
- [Shallow Zarr](https://nl-bioimaging.github.io/BIOMERO.importer/shallow-zarr/): local shallowing and importing remotely shallowed results.
- [NL-BIOMERO deployment guide](https://nl-bioimaging.github.io/NL-BIOMERO/): deploying the complete platform.
- [Release history](https://github.com/NL-BioImaging/BIOMERO.importer/releases).

## Getting started

Deploy the importer with the shared storage, OMERO and PostgreSQL settings
described in the [administration guide](docs/administration.md). The NL-BIOMERO
setup provides the container and its service configuration.

For Python clients:

```sh
pip install 'biomero-importer[identity]'
```

Configure the database and OMERO connection before submitting an order.
Use `get_importer_capabilities()` and `submit_import_order()` from
`biomero_importer`; see the [import-order guide](docs/import-orders.md).

Shallow storage is opt-in. Enabling it permits explicit shallow import
operations; it does not deduplicate every ordinary upload. See the
[shallow-storage guide](docs/shallow-zarr.md).

## Development

See [development and migrations](docs/development.md) for environment setup,
tests and database changes. Documentation uses MkDocs:

```sh
pip install -r docs/requirements.txt
mkdocs serve
```

## License

GPL-2.0; see [LICENSE](LICENSE).
