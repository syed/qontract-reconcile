import copy
import datetime
import hashlib
import json
import re

from threading import Lock

import semver


class ResourceKeyExistsError(Exception):
    pass


class ConstructResourceError(Exception):
    def __init__(self, msg):
        super().__init__("error constructing openshift resource: " + str(msg))


# Regexes for kubernetes objects fields which have to adhere to DNS-1123
DNS_SUBDOMAIN_MAX_LENGTH = 253
DNS_SUBDOMAIN_RE = re.compile(
    r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$')
DNS_LABEL_MAX_LENGTH = 63
DNS_LABEL_RE = re.compile(r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?$')
DNS_NAMES_URL = \
    'https://kubernetes.io/docs/concepts/overview/working-with-objects/names/'

IGNORABLE_DATA_FIELDS = ['service-ca.crt']


class OpenshiftResource:
    def __init__(self, body, integration, integration_version,
                 error_details='', caller_name=None):
        self.body = body
        self.integration = integration
        self.integration_version = integration_version
        self.error_details = error_details
        self.caller_name = caller_name
        self.verify_valid_k8s_object()

    def __eq__(self, other):
        return self.obj_intersect_equal(self.body, other.body)

    def obj_intersect_equal(self, obj1, obj2):
        # obj1 == d_item
        # obj2 == c_item
        if obj1.__class__ != obj2.__class__:
            return False

        if isinstance(obj1, dict):
            for obj1_k, obj1_v in obj1.items():
                obj2_v = obj2.get(obj1_k, None)
                if obj2_v is None:
                    if obj1_v not in [None, '']:
                        return False
                if self.ignorable_field(obj1_k):
                    pass
                elif self.ignorable_key_value_pair(obj1_k, obj1_v):
                    pass
                elif obj1_k in ['data', 'labels', 'matchLabels']:
                    diff = [k for k in obj2_v
                            if k not in obj1_v
                            and k not in IGNORABLE_DATA_FIELDS]
                    if diff or not self.obj_intersect_equal(obj1_v, obj2_v):
                        return False
                elif obj1_k == 'cpu':
                    equal = self.cpu_equal(obj1_v, obj2_v)
                    if not equal:
                        return False
                elif obj1_k == 'apiVersion':
                    valid = self.api_version_mutation(obj1_v, obj2_v)
                    if not valid:
                        return False
                elif obj1_k == 'imagePullSecrets':
                    # remove default pull secrets added by k8s
                    obj2_v_clean = [s for s in obj2_v
                                    if '-dockercfg-' not in s['name']]
                    if not self.obj_intersect_equal(obj1_v, obj2_v_clean):
                        return False
                elif not self.obj_intersect_equal(obj1_v, obj2_v):
                    return False

        elif isinstance(obj1, list):
            if len(obj1) != len(obj2):
                return False
            for index, item in enumerate(obj1):
                if not self.obj_intersect_equal(item, obj2[index]):
                    return False

        elif obj1 != obj2:
            return False

        return True

    @staticmethod
    def ignorable_field(val):
        ignorable_fields = [
            'kubectl.kubernetes.io/last-applied-configuration',
            'creationTimestamp',
            'resourceVersion',
            'generation',
            'selfLink',
            'uid',
            'status',
            'fieldRef'
        ]
        if val in ignorable_fields:
            return True
        return False

    @staticmethod
    def ignorable_key_value_pair(key, val):
        ignorable_key_value_pair = {
            'annotations': None,
            'divisor': '0'
        }
        if key in ignorable_key_value_pair and \
                ignorable_key_value_pair[key] == val:
            return True
        return False

    @staticmethod
    def cpu_equal(val1, val2):
        # normalize both to string
        try:
            val1 = f"{int(float(val1) * 1000)}m"
        except Exception:
            pass
        try:
            val2 = f"{int(float(val2) * 1000)}m"
        except Exception:
            pass
        return val1 == val2

    @staticmethod
    def api_version_mutation(val1, val2):
        # required temporarily, pending response on
        # https://redhat.service-now.com/surl.do?n=INC1224482
        if val1 == 'apps/v1' and val2 == 'extensions/v1beta1':
            return True
        if val1 == 'extensions/v1beta1' and val2 == 'apps/v1':
            return True
        if val1 == 'networking.k8s.io/v1' and val2 == 'extensions/v1beta1':
            return True
        return val1 == val2

    @property
    def name(self):
        return self.body['metadata']['name']

    @property
    def kind(self):
        return self.body['kind']

    @property
    def caller(self):
        try:
            return self.caller_name or \
                self.body['metadata']['annotations']['qontract.caller_name']
        except KeyError:
            return None

    def verify_valid_k8s_object(self):
        try:
            self.name
            self.kind
        except (KeyError, TypeError) as e:
            msg = "resource invalid data ({}). details: {}".format(
                e.__class__.__name__, self.error_details)
            raise ConstructResourceError(msg)

        if self.kind not in \
                ['Role', 'RoleBinding', 'ClusterRole', 'ClusterRoleBinding'] \
                and (not DNS_SUBDOMAIN_RE.match(self.name) or
                     not len(self.name) <= DNS_SUBDOMAIN_MAX_LENGTH):
            msg = f"The {self.kind} \"{self.name}\" is invalid: " + \
                f"metadata.name: Invalid value: \"{self.name}\". " + \
                "This field must adhere to DNS-1123 subdomain names spec." + \
                f"More info can be found at {DNS_NAMES_URL}."
            raise ConstructResourceError(msg)

        # All objects that have a spec.template.spec.containers[]
        try:
            containers = self.body['spec']['template']['spec']['containers']
            if not isinstance(containers, list):
                msg = f"The {self.kind} \"{self.name}\" is invalid: " + \
                      "spec.template.spec.containers is not a list"
                raise ConstructResourceError(msg)
            for c in containers:
                cname = c.get('name', None)
                if cname is None:
                    msg = f"The {self.kind} \"{self.name}\" is invalid: " + \
                        "an item in spec.template.spec.containers was " + \
                        "found without a required name field"
                    raise ConstructResourceError(msg)
                if (not DNS_LABEL_RE.match(cname) or
                        not len(cname) <= DNS_LABEL_MAX_LENGTH):
                    msg = f"The {self.kind} \"{self.name}\" is invalid: " + \
                        "an container in spec.template.spec.containers " + \
                        f"was found with an invalid name ({cname}). More " + \
                        f"info at {DNS_NAMES_URL}."
                    raise ConstructResourceError(msg)
        except KeyError:
            pass

    def has_qontract_annotations(self):
        try:
            annotations = self.body['metadata']['annotations']

            assert annotations['qontract.integration'] == self.integration

            integration_version = annotations['qontract.integration_version']
            assert semver.VersionInfo.parse(integration_version).major == \
                semver.VersionInfo.parse(self.integration_version).major

            assert annotations['qontract.sha256sum'] is not None
        except KeyError:
            return False
        except AssertionError:
            return False
        except ValueError:
            # raised by semver.VersionInfo.parse
            return False

        return True

    def has_owner_reference(self):
        return bool(self.body['metadata'].get('ownerReferences', []))

    def has_valid_sha256sum(self):
        try:
            current_sha256sum = \
                self.body['metadata']['annotations']['qontract.sha256sum']
            return current_sha256sum == self.sha256sum()
        except KeyError:
            return False

    def annotate(self):
        """
        Creates a OpenshiftResource with the qontract annotations, and removes
        unneeded Openshift fields.

        Returns:
            openshift_resource: new OpenshiftResource object with
                annotations.
        """

        # calculate sha256sum of canonical body
        canonical_body = self.canonicalize(self.body)
        sha256sum = self.calculate_sha256sum(self.serialize(canonical_body))

        # create new body object
        body = copy.deepcopy(self.body)

        # create annotations if not present
        body['metadata'].setdefault('annotations', {})
        if body['metadata']['annotations'] is None:
            body['metadata']['annotations'] = {}

        annotations = body['metadata']['annotations']

        # add qontract annotations
        annotations['qontract.integration'] = self.integration
        annotations['qontract.integration_version'] = \
            self.integration_version
        annotations['qontract.sha256sum'] = sha256sum
        now = datetime.datetime.utcnow().replace(microsecond=0).isoformat()
        annotations['qontract.update'] = now
        if self.caller_name:
            annotations['qontract.caller_name'] = self.caller_name

        return OpenshiftResource(body, self.integration,
                                 self.integration_version)

    def sha256sum(self):
        body = self.annotate().body

        annotations = body['metadata']['annotations']
        return annotations['qontract.sha256sum']

    def toJSON(self):
        return self.serialize(self.body)

    @staticmethod
    def canonicalize(body):
        body = copy.deepcopy(body)

        # create annotations if not present
        body['metadata'].setdefault('annotations', {})
        if body['metadata']['annotations'] is None:
            body['metadata']['annotations'] = {}
        annotations = body['metadata']['annotations']

        # remove openshift specific params
        body['metadata'].pop('creationTimestamp', None)
        body['metadata'].pop('resourceVersion', None)
        body['metadata'].pop('generation', None)
        body['metadata'].pop('selfLink', None)
        body['metadata'].pop('uid', None)
        body['metadata'].pop('namespace', None)
        body['metadata'].pop('managedFields', None)
        annotations.pop('kubectl.kubernetes.io/last-applied-configuration',
                        None)

        # remove status
        body.pop('status', None)

        # Default fields for specific resource types
        # ConfigMaps and Secrets are by default Opaque
        if body['kind'] in ('ConfigMap', 'Secret') and \
                body.get('type') == 'Opaque':
            body.pop('type')

        if body['kind'] == 'Deployment':
            annotations.pop('deployment.kubernetes.io/revision', None)

        if body['kind'] == 'Route':
            if body['spec'].get('wildcardPolicy') == 'None':
                body['spec'].pop('wildcardPolicy')
            # remove tls-acme specific params from Route
            if 'kubernetes.io/tls-acme' in annotations:
                annotations.pop(
                    'kubernetes.io/tls-acme-awaiting-authorization-owner',
                    None)
                annotations.pop(
                    'kubernetes.io/tls-acme-awaiting-authorization-at-url',
                    None)
                if 'tls' in body['spec']:
                    tls = body['spec']['tls']
                    tls.pop('key', None)
                    tls.pop('certificate', None)
            subdomain = body['spec'].get('subdomain', None)
            if subdomain == '':
                body['spec'].pop('subdomain', None)

        if body['kind'] == 'ServiceAccount':
            if 'imagePullSecrets' in body:
                # remove default pull secrets added by k8s
                imagePullSecrets = \
                    [s for s in body.pop('imagePullSecrets')
                     if '-dockercfg-' not in s['name']]
                if imagePullSecrets:
                    body['imagePullSecrets'] = imagePullSecrets
            if 'secrets' in body:
                body.pop('secrets')

        if body['kind'] == 'Role':
            for rule in body['rules']:
                if 'resources' in rule:
                    rule['resources'].sort()

                if 'verbs' in rule:
                    rule['verbs'].sort()

                if 'attributeRestrictions' in rule and \
                        not rule['attributeRestrictions']:
                    rule.pop('attributeRestrictions')
            # TODO: remove this once we have no 3.11 clusters
            if body['apiVersion'] == 'authorization.openshift.io/v1':
                body['apiVersion'] = 'rbac.authorization.k8s.io/v1'

        if body['kind'] == 'OperatorGroup':
            annotations.pop('olm.providedAPIs', None)

        if body['kind'] == 'RoleBinding':
            if 'groupNames' in body:
                body.pop('groupNames')
            if 'userNames' in body:
                body.pop('userNames')
            if 'roleRef' in body:
                roleRef = body['roleRef']
                if 'namespace' in roleRef:
                    roleRef.pop('namespace')
                if 'apiGroup' in roleRef and \
                        roleRef['apiGroup'] in body['apiVersion']:
                    roleRef.pop('apiGroup')
                if 'kind' in roleRef:
                    roleRef.pop('kind')
            for subject in body['subjects']:
                if 'namespace' in subject:
                    subject.pop('namespace')
                if 'apiGroup' in subject and \
                        (subject['apiGroup'] == '' or
                            subject['apiGroup'] in body['apiVersion']):
                    subject.pop('apiGroup')
            # TODO: remove this once we have no 3.11 clusters
            if body['apiVersion'] == 'rbac.authorization.k8s.io/v1':
                body['apiVersion'] = 'authorization.openshift.io/v1'

        if body['kind'] == 'ClusterRoleBinding':
            # TODO: remove this once we have no 3.11 clusters
            if body['apiVersion'] == 'authorization.openshift.io/v1':
                body['apiVersion'] = 'rbac.authorization.k8s.io/v1'
            if 'userNames' in body:
                body.pop('userNames')
            if 'roleRef' in body:
                roleRef = body['roleRef']
                if 'apiGroup' in roleRef and \
                        roleRef['apiGroup'] in body['apiVersion']:
                    roleRef.pop('apiGroup')
                if 'kind' in roleRef:
                    roleRef.pop('kind')
            if 'groupNames' in body:
                body.pop('groupNames')
        if body['kind'] == 'Service':
            spec = body['spec']
            if spec.get('sessionAffinity') == 'None':
                spec.pop('sessionAffinity')
            if spec.get('type') == 'ClusterIP':
                spec.pop('clusterIP', None)

        # remove qontract specific params
        annotations.pop('qontract.integration', None)
        annotations.pop('qontract.integration_version', None)
        annotations.pop('qontract.sha256sum', None)
        annotations.pop('qontract.update', None)
        annotations.pop('qontract.caller_name', None)

        return body

    @staticmethod
    def serialize(body):
        return json.dumps(body, sort_keys=True)

    @staticmethod
    def calculate_sha256sum(body):
        m = hashlib.sha256()
        m.update(body.encode('utf-8'))
        return m.hexdigest()


class ResourceInventory:
    def __init__(self):
        self._clusters = {}
        self._error_registered = False
        self._error_registered_clusters = {}
        self._lock = Lock()

    def initialize_resource_type(self, cluster, namespace, resource_type):
        self._clusters.setdefault(cluster, {})
        self._clusters[cluster].setdefault(namespace, {})
        self._clusters[cluster][namespace].setdefault(resource_type, {
            'current': {},
            'desired': {}
        })

    def add_desired(self, cluster, namespace, resource_type, name, value):
        with self._lock:
            desired = \
                (self._clusters[cluster][namespace][resource_type]
                    ['desired'])
            if name in desired:
                raise ResourceKeyExistsError(name)
            desired[name] = value

    def add_current(self, cluster, namespace, resource_type, name, value):
        with self._lock:
            current = \
                (self._clusters[cluster][namespace][resource_type]
                    ['current'])
            current[name] = value

    def __iter__(self):
        for cluster in self._clusters:
            for namespace in self._clusters[cluster]:
                for resource_type in self._clusters[cluster][namespace]:
                    data = self._clusters[cluster][namespace][resource_type]
                    yield (cluster, namespace, resource_type, data)

    def register_error(self, cluster=None):
        self._error_registered = True
        if cluster is not None:
            self._error_registered_clusters[cluster] = True

    def has_error_registered(self, cluster=None):
        if cluster is not None:
            return self._error_registered_clusters.get(cluster, False)
        return self._error_registered
