# Diatreme sets these for a release: VERSION (pr-<N> on a pull request, the release tag
# after a merge), REGISTRY and IMAGE_NAME (<owner>/<repository>).
variable "VERSION" { default = "latest" }
variable "REGISTRY" { default = "ghcr.io" }
variable "IMAGE_NAME" { default = "magmamoose/k6-ui" }
variable "PLATFORMS" { default = "linux/amd64,linux/arm64" }

group "default" {
  targets = ["app"]
}

target "app" {
  context    = "."
  dockerfile = "Dockerfile"
  platforms  = split(",", PLATFORMS)
  tags       = ["${REGISTRY}/${IMAGE_NAME}:${VERSION}"]
  args = {
    VERSION = "${VERSION}"
  }
  labels = {
    "org.opencontainers.image.source"      = "https://github.com/MagmaMoose/k6-ui"
    "org.opencontainers.image.description" = "Run defined k6 load tests on Kubernetes"
    "org.opencontainers.image.licenses"    = "Apache-2.0"
  }
}

# Local single-arch build that loads into Docker: docker buildx bake app-local
target "app-local" {
  inherits  = ["app"]
  platforms = []
  tags      = ["k6-ui:dev"]
  output    = ["type=docker"]
}
