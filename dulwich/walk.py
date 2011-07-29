# walk.py -- General implementation of walking commits and their contents.
# Copyright (C) 2010 Google, Inc.
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; version 2
# or (at your option) any later version of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
# MA  02110-1301, USA.

"""General implementation of walking commits and their contents."""

import heapq
import itertools
import os

from dulwich.diff_tree import (
    RENAME_CHANGE_TYPES,
    tree_changes,
    tree_changes_for_merge,
    RenameDetector,
    )
from dulwich.errors import (
    MissingCommitError,
    )

ORDER_DATE = 'date'

# Maximum number of commits to walk past a commit time boundary.
_MAX_EXTRA_COMMITS = 5


class WalkEntry(object):
    """Object encapsulating a single result from a walk."""

    def __init__(self, walker, commit):
        self.commit = commit
        self._store = walker.store
        self._changes = None
        self._rename_detector = walker.rename_detector

    def changes(self):
        """Get the tree changes for this entry.

        :return: For commits with up to one parent, a list of TreeChange
            objects; if the commit has no parents, these will be relative to the
            empty tree. For merge commits, a list of lists of TreeChange
            objects; see dulwich.diff.tree_changes_for_merge.
        """
        if self._changes is None:
            commit = self.commit
            if not commit.parents:
                changes_func = tree_changes
                parent = None
            elif len(commit.parents) == 1:
                changes_func = tree_changes
                parent = self._store[commit.parents[0]].tree
            else:
                changes_func = tree_changes_for_merge
                parent = [self._store[p].tree for p in commit.parents]
            self._changes = list(changes_func(
              self._store, parent, commit.tree,
              rename_detector=self._rename_detector))
        return self._changes

    def __repr__(self):
        return '<WalkEntry commit=%s, changes=%r>' % (
          self.commit.id, self.changes())


class _CommitTimeQueue(object):
    """Priority queue of WalkEntry objects by commit time."""

    def __init__(self, walker):
        self._walker = walker
        self._store = walker.store
        self._excluded = walker.excluded
        self._pq = []
        self._pq_set = set()
        self._done = set()
        self._min_time = walker.since
        self._extra_commits_left = _MAX_EXTRA_COMMITS

        for commit_id in itertools.chain(walker.include, walker.excluded):
            self._push(commit_id)

    def _push(self, commit_id):
        try:
            commit = self._store[commit_id]
        except KeyError:
            raise MissingCommitError(commit_id)
        if commit_id not in self._pq_set and commit_id not in self._done:
            heapq.heappush(self._pq, (-commit.commit_time, commit))
            self._pq_set.add(commit_id)

    def next(self):
        while self._pq:
            _, commit = heapq.heappop(self._pq)
            sha = commit.id
            self._pq_set.remove(sha)
            if sha in self._done:
                continue

            is_excluded = sha in self._excluded
            if is_excluded:
                self._excluded.update(commit.parents)

            self._done.add(commit.id)
            if self._min_time is not None:
                if commit.commit_time < self._min_time:
                    # We want to stop walking at min_time, but commits at the
                    # boundary may be out of order with respect to their
                    # parents. So we walk _MAX_EXTRA_COMMITS more commits once
                    # we hit this boundary.
                    self._extra_commits_left -= 1
                    if not self._extra_commits_left:
                        break
                else:
                    # We're not at a boundary, so reset the counter.
                    self._extra_commits_left = _MAX_EXTRA_COMMITS

            for parent_id in commit.parents:
                self._push(parent_id)

            if not is_excluded:
                return WalkEntry(self._walker, commit)
        return None


class Walker(object):
    """Object for performing a walk of commits in a store.

    Walker objects are initialized with a store and other options and can then
    be treated as iterators of Commit objects.
    """

    def __init__(self, store, include, exclude=None, order=ORDER_DATE,
                 reverse=False, max_entries=None, paths=None,
                 rename_detector=None, follow=False, since=None, until=None,
                 queue_cls=_CommitTimeQueue):
        """Constructor.

        :param store: ObjectStore instance for looking up objects.
        :param include: Iterable of SHAs of commits to include along with their
            ancestors.
        :param exclude: Iterable of SHAs of commits to exclude along with their
            ancestors, overriding includes.
        :param order: ORDER_* constant specifying the order of results. Anything
            other than ORDER_DATE may result in O(n) memory usage.
        :param reverse: If True, reverse the order of output, requiring O(n)
            memory.
        :param max_entries: The maximum number of entries to yield, or None for
            no limit.
        :param paths: Iterable of file or subtree paths to show entries for.
        :param rename_detector: diff.RenameDetector object for detecting
            renames.
        :param follow: If True, follow path across renames/copies. Forces a
            default rename_detector.
        :param since: Timestamp to list commits after.
        :param until: Timestamp to list commits before.
        :param queue_cls: A class to use for a queue of commits, supporting the
            iterator protocol. The constructor takes a single argument, the
            Walker.
        """
        if order not in (ORDER_DATE,):
            raise ValueError('Unknown walk order %s' % order)
        self.store = store
        self.include = include
        self.excluded = set(exclude or [])
        self.order = order
        self.reverse = reverse
        self.max_entries = max_entries
        self.paths = paths and set(paths) or None
        if follow and not rename_detector:
            rename_detector = RenameDetector(store)
        self.rename_detector = rename_detector
        self.follow = follow
        self.since = since
        self.until = until

        self._num_entries = 0
        self._queue = queue_cls(self)

    def _path_matches(self, changed_path):
        if changed_path is None:
            return False
        for followed_path in self.paths:
            if changed_path == followed_path:
                return True
            if (changed_path.startswith(followed_path) and
                changed_path[len(followed_path)] == '/'):
                return True
        return False

    def _change_matches(self, change):
        old_path = change.old.path
        new_path = change.new.path
        if self._path_matches(new_path):
            if self.follow and change.type in RENAME_CHANGE_TYPES:
                self.paths.add(old_path)
                self.paths.remove(new_path)
            return True
        elif self._path_matches(old_path):
            return True
        return False

    def _should_return(self, entry):
        """Determine if a walk entry should be returned..

        :param entry: The WalkEntry to consider.
        :return: True if the WalkEntry should be returned by this walk, or False
            otherwise (e.g. if it doesn't match any requested paths).
        """
        commit = entry.commit
        if self.since is not None and commit.commit_time < self.since:
            return False
        if self.until is not None and commit.commit_time > self.until:
            return False

        if self.paths is None:
            return True

        if len(commit.parents) > 1:
            for path_changes in entry.changes():
                # For merge commits, only include changes with conflicts for
                # this path. Since a rename conflict may include different
                # old.paths, we have to check all of them.
                for change in path_changes:
                    if self._change_matches(change):
                        return True
        else:
            for change in entry.changes():
                if self._change_matches(change):
                    return True
        return None

    def _next(self):
        max_entries = self.max_entries
        while True:
            if max_entries is not None and self._num_entries >= max_entries:
                return None
            entry = self._queue.next()
            if entry is None:
                return None
            if self._should_return(entry):
                self._num_entries += 1
                return entry

    def __iter__(self):
        results = iter(self._next, None)
        if self.reverse:
            results = reversed(list(results))
        return iter(results)
