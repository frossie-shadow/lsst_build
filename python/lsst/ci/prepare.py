#############################################################################
# Preparer

import os, os.path
import sys
import eups
import hashlib
import shutil
import time
import re
import pipes
import subprocess

import tsort

from .git import Git, GitError

class Preparer(object):
	def __init__(self, build_dir, refs, repository_patterns, sha_abbrev_len, no_pull, exclusions):
		self.build_dir = os.path.abspath(build_dir)
		self.refs = refs
		self.repository_patterns = repository_patterns.split('|')
		self.sha_abbrev_len = sha_abbrev_len
		self.no_pull = no_pull
		self.exclusions = exclusions

		self.deps = []
		self.versions = {}

	def _origin_candidates(self, product):
		""" Expand repository_patterns into URLs. """
		data = { 'product': product }
		return [ pat % data for pat in self.repository_patterns ]

	def _mirror(self, product):
		t0 = time.time()
		sys.stderr.write("%20s: " % product)

		productdir = os.path.join(self.build_dir, product)
		git = Git(productdir)

		# verify the URL of origin hasn't changed
		if os.path.isdir(productdir):
			origin = git('config', '--get', 'remote.origin.url')
			if origin not in self._origin_candidates(product):
				shutil.rmtree(productdir)

		# clone
		if not os.path.isdir(productdir):
			for url in self._origin_candidates(product):
				if not Git.clone(url, productdir, return_status=True)[1]:
					break
			else:
				raise Exception("Failed to clone product '%s' from any of the offered repositories" % product)

		# update from origin
		git("fetch", "origin", "--force", "--prune")
		git("fetch", "origin", "--force", "--tags")

		# find a ref that matches, checkout it
		for ref in self.refs:
			sha1, _ = git("rev-parse", "-q", "--verify", "refs/remotes/origin/" + ref, return_status=True)
			#print ref, "branch=", sha1
			branch = sha1 != ""
			if not sha1:
				sha1, _ = git("rev-parse", "-q", "--verify", "refs/tags/" + ref + "^0", return_status=True)
			if not sha1:
				sha1, _ = git("rev-parse", "-q", "--verify", "__dummy-g" + ref, return_status=True)
			if not sha1:
				continue

			git("checkout", "--force", ref)

			if branch:
				git("pull")

			#print "HEAD=", git("rev-parse", "HEAD")
			assert(git("rev-parse", "HEAD") == sha1)
			break
		else:
			raise Exception("None of the specified refs exist in product '%s'" % product)

		git("reset", "--hard")
		git("clean", "-d", "-f", "-q")

		print >>sys.stderr, " ok (%.1f sec)." % (time.time() - t0)
		return ref, sha1

	def _prepare(self, product):
		try:
			return self.versions[product]
		except KeyError:
			pass

		ref, sha1 = self._mirror(product)

		# Parse the table file to discover dependencies
		productdir = os.path.join(self.build_dir, product)
		dep_vers = []
		table_fn = os.path.join(productdir, 'ups', '%s.table' % product)
		if os.path.isfile(table_fn):
			# Choose which dependencies to prepare
			product_deps = []
			for dep in eups.table.Table(table_fn).dependencies(eups.Eups()):
				if dep[1] == True and self._is_excluded(dep[0].name, product):	# skip excluded optionals
					continue;
				if dep[0].name == "implicitProducts": continue;			# skip implicit products
				product_deps.append(dep[0].name)

			# Recursively prepare the chosen dependencies
			for dep_product in product_deps:
				dep_ver = self._prepare(dep_product)[0]
				dep_vers.append(dep_ver)
				self.deps.append((dep_product, product))

		# Construct EUPS version
		version = self._construct_version(productdir, ref, dep_vers)

		# Store the result
		self.versions[product] = (version, sha1)
		
		return self.versions[product]

	def _construct_version(self, productdir, ref, dep_versions):
		""" Return a standardized XXX+YYY EUPS version, that includes the dependencies. """
		q = pipes.quote
		cmd ="cd %s && pkgbuild -f git_version %s" % (q(productdir), q(ref))
		ver = subprocess.check_output(cmd, shell=True).strip()

		if dep_versions:
			deps_sha1 = self._depver_hash(dep_versions)
			return "%s+%s" % (ver, deps_sha1)
		else:
			return ver

	def _is_excluded(self, dep, product):
		""" Check if dependency 'dep' is excluded for product 'product' """
		try:
			rc = self.exclusion_regex_cache
		except AttributeError:
			rc = self.exclusion_regex_cache = dict()

		if product not in rc:
			rc[product] = [ dep_re for (dep_re, prod_re) in self.exclusions if prod_re.match(product) ]
		
		for dep_re in rc[product]:
			if dep_re.match(dep):
				return True

		return False

	def _depver_hash(self, versions):
		""" Return a standardized hash of the list of versions """
		return hashlib.sha1('\n'.join(sorted(versions))).hexdigest()[:self.sha_abbrev_len]

	@staticmethod
	def run(args):
		# Ensure build directory exists and is writable
		build_dir = args.build_dir
		if not os.access(build_dir, os.W_OK):
			raise Exception("Directory '%s' does not exist or isn't writable." % build_dir)

		# Add 'master' to list of refs, if not there already
		refs = args.ref
		if 'master' not in refs:
			refs.append('master')

		# Load exclusion map
		exclusions = []
		if args.exclusion_map:
			with open(args.exclusion_map) as fp:
				for line in fp:
					line = line.strip()
					if not line or line.startswith("#"):
						continue
					(dep_re, prod_re) = line.split()[:2]
					exclusions.append((re.compile(dep_re), re.compile(prod_re)))

		# Prepare products
		p = Preparer(build_dir, refs, args.repository_pattern, args.sha_abbrev_len, args.no_pull, exclusions)
		for product in args.products:
			p._prepare(product)

		# Topologically sort the result, add any products that have no dependencies
		products = tsort.tsort(p.deps)
		_p = set(products)
		for product in set(args.products):
			if product not in _p:
				products.append(product)

		print '# %-23s %-41s %-30s' % ("product", "SHA1", "Version")
		for product in products:
			print '%-25s %-41s %-30s' % (product, p.versions[product][1], p.versions[product][0])

