#!/usr/bin/env python
"""DDC 304 was incorrectly classified under Periodicals. 304 is Social Sciences, 305 is Periodicals.

That has now been fixed.

Correct all 304.* and 305.* subjects, and reclassify every work classified under those IDs.
"""
import os
import sys
from pdb import set_trace
bin_dir = os.path.split(__file__)[0]
package_dir = os.path.join(bin_dir, "..", "..")
sys.path.append(os.path.abspath(package_dir))
from core.model import (
    production_session, 
    Edition,
    Identifier,
    Work,
    Genre,
    WorkGenre,
    Subject,
)

_db = production_session()

def reclassify(ddc):
    for subject in _db.query(Subject).filter(Subject.type==Subject.DDC).filter(Subject.identifier.like(ddc)):
        subject.assign_to_genre()
        for cl in subject.classifications:
            ids = cl.identifier.equivalent_identifier_ids()
            print "Looking for editions associated with %d ids." % len(ids)
            editions = _db.query(Edition).filter(Edition.primary_identifier_id.in_(ids)).all()
            for edition in editions:
                if edition.work:
                    print "OLD: %r" % edition.work.genres
                    edition.work.calculate_presentation()
                    print "NEW: %r" % edition.work.genres
                else:
                    edition.calculate_presentation()
        _db.commit()

reclassify("304")
reclassify("305")
