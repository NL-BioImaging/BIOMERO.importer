# Container security

The importer accesses OMERO credentials, the import database and shared image
storage. Restrict those credentials, mounts and database permissions to the
service's requirements.

## Nested Podman

The container runs as `autoimportuser` and uses rootless Podman to start
preprocessing containers. For a Linux rootless Podman deployment using VFS
storage, the current image supports:

```sh
podman run --rm --name biomero-importer \
  --userns=keep-id:uid=1000,gid=1000 \
  --security-opt label=disable \
  --env-file importer.env \
  --volume /mnt/shared-storage:/data \
  --volume /mnt/omero:/OMERO \
  --volume /mnt/importer-config:/auto-importer/config \
  cellularimagingcf/biomero-importer:YOUR_RELEASE_TAG
```

Replace the paths and image tag with your deployment values. This rootless
VFS configuration does not require `--privileged` or `/dev/fuse`.
Other runtimes and storage drivers have different requirements; use the
[NL-BIOMERO deployment guide](https://nl-bioimaging.github.io/NL-BIOMERO/)
for the supplied Docker/Windows setup.

`label=disable` disables SELinux container label separation for this
container. User namespaces do not replace filesystem access controls or make
broad writable host mounts safe. Limit mounts to the data and configuration
needed by the importer.

Avoid overlapping nested-container mounts, particularly mounting both a
directory and a file inside that directory. Keep configuration-file targets
separate when using rootless Podman VFS.

## Shared data

In-place registration requires consistent paths and permissions between the
importer and OMERO. Canonical pixels referenced by shallow results must remain
available. Do not rename or delete those sources as routine cleanup.

Preprocessing runs code selected by your configured container images. Use
trusted images and restrict who can submit import orders or change preprocessing
configuration. Remote shallow receipts assume trusted workflow orchestration;
they are not signed attestations against a malicious workflow. See the
[shallower trust model](https://nl-bioimaging.github.io/BIOMERO.shallower/architecture/#trust-and-portability).
