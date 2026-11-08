"""Revocable tool dispatch; ending an episode drains admitted operations."""
import json
from copy import deepcopy
from threading import Condition
from jsonschema import Draft202012Validator, ValidationError


class AgentAccessClosed(RuntimeError):
    """The call was not admitted because this episode has ended."""


class AgentEnvironment:
    def __init__(self, definitions, dispatch):
        self.__dispatch = dispatch
        self.__validators = {}
        for definition in definitions:
            if definition.name in self.__validators:
                raise ValueError('Duplicate task tool: ' + definition.name)
            schema = deepcopy(dict(definition.input_schema))
            Draft202012Validator.check_schema(schema)
            self.__validators[definition.name] = Draft202012Validator(schema)
        self.__condition = Condition()
        self.__active = False
        self.__inflight = 0

    def begin(self, *, admit_call, event):
        with self.__condition:
            if self.__active or self.__inflight:
                raise RuntimeError('Tool environment is already active')
            self.__admit, self.__event = admit_call, event
            self.__active = True

    def end(self):
        with self.__condition:
            self.__active = False
            while self.__inflight:
                self.__condition.wait()

    def call(self, name, arguments):
        from ..episodes.identity import call_identity
        with self.__condition:
            if not self.__active:
                raise AgentAccessClosed('Agent access is closed')
            self.__admit()
            self.__inflight += 1
        try:
            try:
                arguments = json.loads(json.dumps(arguments, allow_nan=False))
                json_arguments = True
            except (ValueError, TypeError):
                json_arguments = False
            self.__event({'kind': 'task_tool_start', 'tool': name,
                          **call_identity(),
                          'arguments': arguments if json_arguments else None,
                          'invalid_json_arguments': not json_arguments})
            if name not in self.__validators:
                result = {'error': {'code': 'unknown_tool', 'message': 'Unknown task tool'}}
            elif not json_arguments:
                result = {'error': {'code': 'invalid_arguments', 'message': 'Arguments must be finite JSON values'}}
            else:
                try:
                    # JSON-only arguments avoid coercion and non-finite numbers across transports.
                    self.__validators[name].validate(arguments)
                except (ValidationError, ValueError, TypeError):
                    result = {'error': {'code': 'invalid_arguments', 'message': 'Arguments do not match the tool schema'}}
                else:
                    result = self.__dispatch(name, arguments)
            self.__event({'kind': 'task_tool_end', 'tool': name, 'result': result, **call_identity()})
            return result
        except BaseException as exc:
            self.__event({'kind': 'task_tool_error', 'tool': name, **call_identity(), 'error_type': type(exc).__name__})
            raise
        finally:
            with self.__condition:
                self.__inflight -= 1
                self.__condition.notify_all()
