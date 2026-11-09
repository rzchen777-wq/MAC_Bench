"""Grade generated artifacts in a fresh consumer, never in the source workspace."""
import json
from .product.runtime import ProductRuntime
from .product.archives import check_archive
from .product.artifacts import write_json
from .product.verifier import score_product


class ArtifactRuntime(ProductRuntime):
    def open(self, *, prepared=None, network_blocked=False):
        if not prepared:
            raise ValueError('Artifact grading requires a clean infrastructure snapshot')
        self.infrastructure_image = prepared
        return super().open(prepared=prepared, network_blocked=network_blocked)

    def verify(self, verifier_directory, contract, *, mode='normal'):
        config = self.environment['artifact_verifier']
        consumer = ProductRuntime(self.environment, self.directory / ('consumer-' + mode), backend=self.backend)
        try:
            self.backend.upload_tree(self.sandbox, local_dir=verifier_directory, remote_dir='/work/grader')
            result = self.command('produce-' + mode, config['producer_command'], timeout=config.get('producer_timeout', 300))
            if result.exit_code != 0:
                raise RuntimeError('Producer verifier failed before returning observations')
            produced = json.loads(self.backend.read_file(self.sandbox, path='/work/grader/producer-result.json'))
            archive = self.directory / ('deliverables-' + mode + '.tar')
            archive.write_bytes(self.read_archive('/work/deliverables.tar'))
            check_archive(archive, external_symlink_targets=config.get('external_symlink_targets', []))
            # Use the infrastructure snapshot, not a snapshot of this producer.
            consumer.open(prepared=self.infrastructure_image, network_blocked=True)
            self.backend.write_file(consumer.sandbox, path='/work/deliverables.tar', content=archive.read_bytes())
            self.backend.upload_tree(consumer.sandbox, local_dir=verifier_directory, remote_dir='/work/checks')
            result = consumer.command('consume', config['consumer_command'], timeout=config.get('consumer_timeout', 120))
            if result.exit_code != 0:
                raise RuntimeError('Consumer verifier failed before returning observations')
            consumed = json.loads(self.backend.read_file(consumer.sandbox, path='/work/checks/consumer-result.json'))
            expected = config['cases']
            for observations in [produced['cases'], consumed['cases']]:
                ids = [case['id'] for case in observations]
                if len(ids) != len(set(ids)) or set(ids) != set(expected):
                    raise ValueError('Missing, duplicate or undeclared artifact cases')
                if any(case['goal'] != expected[case['id']] or case['status'] not in {'passed', 'failed'} for case in observations):
                    raise ValueError('Invalid artifact case observations')
            checks = []
            for goal in contract['goals']:
                cases = [case for case in produced['cases'] + consumed['cases'] if case['goal'] == goal['id']]
                if not cases:
                    raise ValueError('Artifact goal has no cases')
                checks.append({'id': goal['id'], 'status': 'passed' if all(case['status'] == 'passed' for case in cases) else 'failed'})
            checks.append(produced['regression'])
            score = score_product(contract, {'checks': checks})
            write_json(self.directory / ('producer-' + mode + '.json'), produced)
            write_json(self.directory / ('consumer-' + mode + '.json'), consumed)
        except (OSError, ValueError, KeyError, RuntimeError) as error:
            score = {'status': 'verifier_error', 'task_success': None, 'gc': None, 'error': str(error)}
        finally:
            consumer.close()
        write_json(self.directory / ('score-' + mode + '.json'), score)
        return score
