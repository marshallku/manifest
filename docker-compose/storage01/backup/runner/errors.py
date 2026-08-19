class BackupError(Exception):
    """Base for every error this runner raises deliberately."""


class ConfigError(BackupError):
    """config.yaml violates the schema. Nothing runs."""


class JobError(BackupError):
    """One job failed. Other jobs still run; the snapshot does not."""
