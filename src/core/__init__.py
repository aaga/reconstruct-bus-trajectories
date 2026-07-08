"""Core business logic: smoothing, trajectory reconstruction, serialization,
map-matching, and delay decomposition. No plotting; the only outward dependency
is on ``dataio`` for shared loaders/types (to be pared back as I/O wrappers move
fully into ``dataio``)."""
