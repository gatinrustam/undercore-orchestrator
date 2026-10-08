"""Versioned connection contract; protocol payloads never appear in repr/errors."""
from typing import Annotated, Literal
from datetime import datetime
from pydantic import AfterValidator, BeforeValidator, BaseModel, ConfigDict, Field, model_validator

ProtocolName = Literal['amneziawg', 'trusttunnel']
Identifier = Annotated[str, Field(pattern=r'^[A-Za-z0-9_-]{1,100}$')]

def integer_version(value):
    if type(value) is not int:
        raise ValueError('Version must be an integer')
    return value


VersionOne = Annotated[Literal[1], BeforeValidator(integer_version)]

def valid_timestamp(value):
    if datetime.fromisoformat(value.replace('Z', '+00:00')).year > 2100:
        raise ValueError('Timestamp out of range')
    return value


Timestamp = Annotated[str, AfterValidator(valid_timestamp), Field(pattern=r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$')]


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, hide_input_in_errors=True)


class Capability(Contract):
    protocol: ProtocolName
    configuration_version: int = Field(ge=1, le=65535)


class ClientCapabilities(Contract):
    schema_version: VersionOne
    capabilities: list[Capability] = Field(min_length=1, max_length=8)

    @model_validator(mode='after')
    def distinct(self):
        pairs = [(c.protocol, c.configuration_version) for c in self.capabilities]
        if len(pairs) != len(set(pairs)):
            raise ValueError('Duplicate capability')
        return self


class DeviceRequest(ClientCapabilities):
    # Canonical device/access ID from Laravel AFTER ownership and slot reservation.
    # This is not the user-supplied installation UUID or an account password.
    device_id: Identifier
    name: str = Field(min_length=1, max_length=80, pattern=r'^[^\x00-\x1f\x7f]+$')
    expires_at: Timestamp


class ConfigurationRequest(ClientCapabilities):
    device_id: Identifier


class RecoveryRequest(ConfigurationRequest):
    expected_revision: int = Field(ge=1, le=2147483647)


class SwitchRequest(ConfigurationRequest):
    expected_node_id: Identifier
    idempotency_key: Identifier


class TransportConfiguration(Contract):
    protocol: ProtocolName
    version: VersionOne = 1
    format: Literal['awg-quick', 'trusttunnel-toml']
    # An internal engine document, never a user-visible file. TrustTunnel gets its
    # own TOML payload, NOT WireGuard fields. Each driver validates its document.
    data: str = Field(min_length=1, max_length=65536, repr=False)

    @model_validator(mode='after')
    def matching_format(self):
        if len(self.data.encode('utf-8')) > 65536 or '\0' in self.data:
            raise ValueError('Invalid configuration size or encoding')
        expected = {'amneziawg': 'awg-quick', 'trusttunnel': 'trusttunnel-toml'}
        if self.format != expected[self.protocol]:
            raise ValueError('Protocol and format do not match')
        return self


class Connection(Contract):
    schema_version: VersionOne = 1
    connection_id: Identifier
    device_id: Identifier
    node_id: Identifier
    protocol: ProtocolName
    state: Literal['pending', 'active', 'disabled', 'expired', 'conflict']
    expires_at: Timestamp


class ConnectionConfiguration(Connection):
    revision: int = Field(ge=1)
    configuration: TransportConfiguration = Field(repr=False)

    @model_validator(mode='after')
    def matching_protocol(self):
        if self.configuration.protocol != self.protocol:
            raise ValueError('Connection and configuration do not match')
        return self


def schemas():
    """Machine-readable contract for the Laravel facade and native client adapters."""
    return {model.__name__: {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                             **model.model_json_schema()}
            for model in (DeviceRequest, ConfigurationRequest, RecoveryRequest, SwitchRequest, Connection, ConnectionConfiguration)}
