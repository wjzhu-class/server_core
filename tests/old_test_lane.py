import datetime
import json

from nose.tools import (
    eq_,
    set_trace,
    assert_raises,
)

from psycopg2.extras import NumericRange

from . import (
    DatabaseTest,
)

import classifier
from classifier import (
    Classifier,
)

from lane import (
    Facets,
    Pagination,
    Lane,
    LaneList,
    UndefinedLane,
)

from config import (
    Configuration, 
    temp_config,
)

from model import (
    get_one_or_create,
    DataSource,
    Edition,
    Genre,
    Identifier,
    Library,
    LicensePool,
    SessionManager,
    Work,
    WorkGenre,
)


class TestLane(DatabaseTest):

    def test_depth(self):
        child = Lane(self._db, self._default_library, "sublane")
        parent = Lane(self._db, self._default_library, "parent", sublanes=[child])
        eq_(0, parent.depth)
        eq_(1, child.depth)

    def test_includes_language(self):
        english_lane = Lane(self._db, self._default_library, self._str, languages=['eng'])
        eq_(True, english_lane.includes_language('eng'))
        eq_(False, english_lane.includes_language('fre'))

        no_english_lane = Lane(self._db, self._default_library, self._str, exclude_languages=['eng'])
        eq_(False, no_english_lane.includes_language('eng'))
        eq_(True, no_english_lane.includes_language('fre'))

        all_language_lane = Lane(self._db, self._default_library, self._str)
        eq_(True, all_language_lane.includes_language('eng'))
        eq_(True, all_language_lane.includes_language('fre'))

    def test_set_customlist_ignored_when_no_list(self):

        class SetCustomListErrorLane(Lane):
            def set_customlist_information(self, *args, **kwargs):
                raise RuntimeError()

        # Because this lane has no list-related information, the
        # RuntimeError shouldn't pop up at all.
        lane = SetCustomListErrorLane(self._db, self._default_library, self._str)

        # The minute we put in some list information, it does!
        assert_raises(
            RuntimeError, SetCustomListErrorLane, self._db, self._default_library,
            self._str, list_data_source=DataSource.NYT
        )

        # It can be a DataSource, or a CustomList identifier. World == oyster.
        assert_raises(
            RuntimeError, SetCustomListErrorLane, self._db, self._default_library,
            self._str, list_identifier=u"Staff Picks"
        )


class TestLanes(DatabaseTest):

    def test_all_matching_genres(self):
        fantasy, ig = Genre.lookup(self._db, classifier.Fantasy)
        cooking, ig = Genre.lookup(self._db, classifier.Cooking)
        matches = Lane.all_matching_genres(self._db, [fantasy, cooking])
        names = sorted([x.name for x in matches])
        eq_(
            [u'Cooking', u'Epic Fantasy', u'Fantasy', u'Historical Fantasy', 
             u'Urban Fantasy'], 
            names
        )

    def test_nonexistent_list_raises_exception(self):
        assert_raises(
            UndefinedLane, Lane, self._db, self._default_library, 
            u"This Will Fail", list_identifier=u"No Such List"
        )

    def test_staff_picks_and_best_sellers_sublane(self):
        staff_picks, ignore = self._customlist(
            foreign_identifier=u"Staff Picks", name=u"Staff Picks!", 
            data_source_name=DataSource.LIBRARY_STAFF,
            num_entries=0
        )
        best_sellers, ignore = self._customlist(
            foreign_identifier=u"NYT Best Sellers", name=u"Best Sellers!", 
            data_source_name=DataSource.NYT,
            num_entries=0
        )
        lane = Lane(
            self._db, self._default_library, "Everything", 
            include_staff_picks=True, include_best_sellers=True
        )

        # A staff picks sublane and a best-sellers sublane have been
        # created for us.
        best, picks = lane.sublanes.lanes
        eq_("Best Sellers", best.display_name)
        eq_("Everything - Best Sellers", best.name)
        nyt = DataSource.lookup(self._db, DataSource.NYT)
        eq_(nyt.id, best.list_data_source_id)

        eq_("Staff Picks", picks.display_name)
        eq_("Everything - Staff Picks", picks.name)
        eq_([staff_picks.id], picks.list_ids)

    def test_custom_list_can_set_featured_works(self):
        my_list = self._customlist(num_entries=4)[0]

        featured_entries = my_list.entries[1:3]
        featured_works = list()
        for entry in featured_entries:
            featured_works.append(entry.edition.work)
            entry.featured = True

        other_works = [e.edition.work for e in my_list.entries if not e.featured]
        for work in other_works:
            # Make the other works feature-quality so they are in the running.
            work.quality = 1.0

        self._db.commit()
        SessionManager.refresh_materialized_views(self._db)

        lane = Lane(self._db, self._default_library, u'My Lane', list_identifier=my_list.foreign_identifier)

        result = lane.list_featured_works_query.all()
        eq_(sorted(featured_works), sorted(result))

        def _assert_featured_works(size, expected_works=None, expected_length=None,
                                   sampled_works=None):
            featured_works = None
            featured_materialized_works = None
            library = self._default_library
            library.setting(library.FEATURED_LANE_SIZE).value = size
            featured_works = lane.featured_works(use_materialized_works=False)
            featured_materialized_works = lane.featured_works()

            expected_length = expected_length
            if expected_length == None:
                expected_length = size
            eq_(expected_length, len(featured_works))
            eq_(expected_length, len(featured_materialized_works))

            expected_works = expected_works or []
            for work in expected_works:
                assert work in featured_works
                # There's also a single MaterializedWork that matches the work.
                [materialized_work] = filter(
                    lambda mw: mw.works_id==work.id, featured_materialized_works
                )

                # Remove the confirmed works for the next test.
                featured_works.remove(work)
                featured_materialized_works.remove(materialized_work)

            sampled_works = sampled_works or []
            for work in featured_works:
                assert work in sampled_works
            for work in featured_materialized_works:
                [sampled_work] = filter(
                    lambda sample: sample.id==work.works_id, sampled_works
                )

        # If the number of featured works completely fills the lane,
        # we only get featured works back.
        _assert_featured_works(2, featured_works)

        # If the number of featured works doesn't fill the lane, a
        # random other work that does will be sampled from the lane's
        # works
        _assert_featured_works(3, featured_works, sampled_works=other_works)

        # If the number of featured works falls slightly below the featured
        # lane size, all the available books are returned, without the
        # CustomList features being duplicated.
        _assert_featured_works(
            5, featured_works, expected_length=4, sampled_works=other_works)

        # If the number of featured works falls far (>5) below the featured
        # lane size, nothing is returned.
        _assert_featured_works(10, expected_length=0)

    def test_gather_matching_genres(self):
        self.fantasy, ig = Genre.lookup(self._db, classifier.Fantasy)
        self.urban_fantasy, ig = Genre.lookup(
            self._db, classifier.Urban_Fantasy
        )

        self.cooking, ig = Genre.lookup(self._db, classifier.Cooking)
        self.history, ig = Genre.lookup(self._db, classifier.History)

        # Fantasy contains three subgenres and is restricted to fiction.
        fantasy, default = Lane.gather_matching_genres(
            self._db, [self.fantasy], Lane.FICTION_DEFAULT_FOR_GENRE
        )
        eq_(4, len(fantasy))
        eq_(True, default)

        fantasy, default = Lane.gather_matching_genres(
            self._db, [self.fantasy], True
        )
        eq_(4, len(fantasy))
        eq_(True, default)

        fantasy, default = Lane.gather_matching_genres(
            self._db, [self.fantasy], True, [self.urban_fantasy]
        )
        eq_(3, len(fantasy))
        eq_(True, default)

        # If there are only exclude_genres available, then it and its
        # subgenres are ignored while every OTHER genre is set.
        genres, default = Lane.gather_matching_genres(
            self._db, [], True, [self.fantasy]
        )
        eq_(False, any([g for g in self.fantasy.self_and_subgenres if g in genres]))
        # According to known fiction status, that is.
        eq_(True, all([g.default_fiction==True for g in genres]))

        # Attempting to create a contradiction (like nonfiction fantasy)
        # will create a lane broad enough to actually contain books
        fantasy, default = Lane.gather_matching_genres(self._db, [self.fantasy], False)
        eq_(4, len(fantasy))
        eq_(Lane.BOTH_FICTION_AND_NONFICTION, default)

        # Fantasy and history have conflicting fiction defaults, so
        # although we can make a lane that contains both, we can't
        # have it use the default value.
        assert_raises(UndefinedLane, Lane.gather_matching_genres,
            self._db, [self.fantasy, self.history], Lane.FICTION_DEFAULT_FOR_GENRE
        )

    def test_subgenres_become_sublanes(self):
        fantasy, ig = Genre.lookup(self._db, classifier.Fantasy)
        lane = Lane(
            self._db, self._default_library, "YA Fantasy", genres=fantasy, 
            languages='eng',
            audiences=Lane.AUDIENCE_YOUNG_ADULT,
            age_range=[15,16],
            subgenre_behavior=Lane.IN_SUBLANES
        )
        sublanes = lane.sublanes.lanes
        names = sorted([x.name for x in sublanes])
        eq_(["Epic Fantasy", "Historical Fantasy", "Urban Fantasy"],
            names)

        # Sublanes inherit settings from their parent.
        assert all([x.languages==['eng'] for x in sublanes])
        assert all([x.age_range==[15, 16] for x in sublanes])
        assert all([x.audiences==set(['Young Adult']) for x in sublanes])

    def test_get_search_target(self):
        fantasy, ig = Genre.lookup(self._db, classifier.Fantasy)
        lane = Lane(
            self._db, self._default_library, "YA Fantasy", genres=fantasy, 
            languages='eng',
            audiences=Lane.AUDIENCE_YOUNG_ADULT,
            age_range=[15,16],
            subgenre_behavior=Lane.IN_SUBLANES
        )
        sublanes = lane.sublanes.lanes
        names = sorted([x.name for x in sublanes])
        eq_(["Epic Fantasy", "Historical Fantasy", "Urban Fantasy"],
            names)

        # To start with, none of the lanes are searchable.
        eq_(None, lane.search_target)
        eq_(None, sublanes[0].search_target)

        # If we make a lane searchable, suddenly there's a search target.
        lane.searchable = True
        eq_(lane, lane.search_target)

        # The searchable lane also becomes the search target for its
        # children.
        eq_(lane, sublanes[0].search_target)

    def test_custom_sublanes(self):
        fantasy, ig = Genre.lookup(self._db, classifier.Fantasy)
        urban_fantasy, ig = Genre.lookup(self._db, classifier.Urban_Fantasy)

        urban_fantasy_lane = Lane(
            self._db, self._default_library, "Urban Fantasy", genres=urban_fantasy)

        fantasy_lane = Lane(
            self._db, self._default_library, "Fantasy", fantasy, 
            genres=fantasy,
            subgenre_behavior=Lane.IN_SAME_LANE,
            sublanes=[urban_fantasy_lane]
        )
        eq_([urban_fantasy_lane], fantasy_lane.sublanes.lanes)

        # You can just give the name of a genre as a sublane and it
        # will work.
        fantasy_lane = Lane(
            self._db, self._default_library, "Fantasy", fantasy, 
            genres=fantasy,
            subgenre_behavior=Lane.IN_SAME_LANE,
            sublanes="Urban Fantasy"
        )
        eq_([["Urban Fantasy"]], [x.genre_names for x in fantasy_lane.sublanes.lanes])

    def test_custom_lanes_conflict_with_subgenre_sublanes(self):

        fantasy, ig = Genre.lookup(self._db, classifier.Fantasy)
        urban_fantasy, ig = Genre.lookup(self._db, classifier.Urban_Fantasy)

        urban_fantasy_lane = Lane(
            self._db, self._default_library, "Urban Fantasy", genres=urban_fantasy)

        assert_raises(UndefinedLane, Lane,
            self._db, self._default_library, "Fantasy", fantasy, 
            genres=fantasy,
            audiences=Lane.AUDIENCE_YOUNG_ADULT,
            subgenre_behavior=Lane.IN_SUBLANES,
            sublanes=[urban_fantasy_lane]
        )

    def test_lane_query_with_configured_opds(self):
        """The appropriate opds entry is deferred during querying.
        """
        original_setting = Configuration.DEFAULT_OPDS_FORMAT
        lane = Lane(self._db, self._default_library, "Everything")

        # Verbose config doesn't query simple OPDS entries.
        Configuration.DEFAULT_OPDS_FORMAT = "verbose_opds_entry"
        works_query_str = str(lane.works())
        mw_query_str = str(lane.materialized_works())
        
        assert "verbose_opds_entry" in works_query_str
        assert "verbose_opds_entry" in mw_query_str
        assert "works.simple_opds_entry" not in works_query_str
        assert "simple_opds_entry" not in mw_query_str

        # Simple config doesn't query verbose OPDS entries.
        Configuration.DEFAULT_OPDS_FORMAT = "simple_opds_entry"
        works_query_str = str(lane.works())
        mw_query_str = str(lane.materialized_works())

        assert "works.simple_opds_entry" in works_query_str
        assert "simple_opds_entry" in mw_query_str
        assert "verbose_opds_entry" not in works_query_str
        assert "verbose_opds_entry" not in mw_query_str

        Configuration.DEFAULT_OPDS_FORMAT = original_setting

    def test_visible_parent(self):
        fantasy, ig = Genre.lookup(self._db, classifier.Fantasy)
        urban_fantasy, ig = Genre.lookup(self._db, classifier.Urban_Fantasy)

        sublane = Lane(
            self._db, self._default_library, "Urban Fantasy", genres=urban_fantasy)

        invisible_parent = Lane(
            self._db, self._default_library, "Fantasy", invisible=True, genres=fantasy, 
            sublanes=[sublane], subgenre_behavior=Lane.IN_SAME_LANE)

        visible_grandparent = Lane(
            self._db, self._default_library, "English", sublanes=[invisible_parent],
            subgenre_behavior=Lane.IN_SAME_LANE)

        eq_(sublane.visible_parent(), visible_grandparent)
        eq_(invisible_parent.visible_parent(), visible_grandparent)
        eq_(visible_grandparent.visible_parent(), None)

    def test_visible_ancestors(self):
        fantasy, ig = Genre.lookup(self._db, classifier.Fantasy)
        urban_fantasy, ig = Genre.lookup(self._db, classifier.Urban_Fantasy)

        lane = Lane(
            self._db, self._default_library, "Urban Fantasy", genres=urban_fantasy)

        visible_parent = Lane(
            self._db, self._default_library, "Fantasy", genres=fantasy,
            sublanes=[lane], subgenre_behavior=Lane.IN_SAME_LANE)

        invisible_grandparent = Lane(
            self._db, self._default_library, "English", invisible=True, sublanes=[visible_parent],
            subgenre_behavior=Lane.IN_SAME_LANE)

        visible_ancestor = Lane(
            self._db, self._default_library, "Books With Words", sublanes=[invisible_grandparent],
            subgenre_behavior=Lane.IN_SAME_LANE)

        eq_(lane.visible_ancestors(), [visible_parent, visible_ancestor])

    def test_has_visible_sublane(self):
        fantasy, ig = Genre.lookup(self._db, classifier.Fantasy)
        urban_fantasy, ig = Genre.lookup(self._db, classifier.Urban_Fantasy)

        sublane = Lane(
            self._db, self._default_library, "Urban Fantasy", genres=urban_fantasy,
            subgenre_behavior=Lane.IN_SAME_LANE)

        invisible_parent = Lane(
            self._db, self._default_library, "Fantasy", invisible=True, genres=fantasy,
            sublanes=[sublane], subgenre_behavior=Lane.IN_SAME_LANE)

        visible_grandparent = Lane(
            self._db, self._default_library, "English", sublanes=[invisible_parent],
            subgenre_behavior=Lane.IN_SAME_LANE)

        eq_(False, visible_grandparent.has_visible_sublane())
        eq_(True, invisible_parent.has_visible_sublane())
        eq_(False, sublane.has_visible_sublane())

    def test_visible_sublanes(self):
        fantasy, ig = Genre.lookup(self._db, classifier.Fantasy)
        urban_fantasy, ig = Genre.lookup(self._db, classifier.Urban_Fantasy)
        humorous, ig = Genre.lookup(self._db, classifier.Humorous_Fiction)

        visible_sublane = Lane(self._db, self._default_library, "Humorous Fiction", genres=humorous)

        visible_grandchild = Lane(
            self._db, self._default_library, "Urban Fantasy", genres=urban_fantasy)

        invisible_sublane = Lane(
            self._db, self._default_library, "Fantasy", invisible=True, genres=fantasy,
            sublanes=[visible_grandchild], subgenre_behavior=Lane.IN_SAME_LANE)

        lane = Lane(
            self._db, self._default_library, "English", sublanes=[visible_sublane, invisible_sublane],
            subgenre_behavior=Lane.IN_SAME_LANE)

        eq_(2, len(lane.visible_sublanes))
        assert visible_sublane in lane.visible_sublanes
        assert visible_grandchild in lane.visible_sublanes


class TestLanesQuery(DatabaseTest):

    def setup(self):
        super(TestLanesQuery, self).setup()

        # Look up the Fantasy genre and some of its subgenres.
        self.fantasy, ig = Genre.lookup(self._db, classifier.Fantasy)
        self.epic_fantasy, ig = Genre.lookup(self._db, classifier.Epic_Fantasy)
        self.urban_fantasy, ig = Genre.lookup(
            self._db, classifier.Urban_Fantasy
        )

        # Look up the History genre and some of its subgenres.
        self.history, ig = Genre.lookup(self._db, classifier.History)
        self.african_history, ig = Genre.lookup(
            self._db, classifier.African_History
        )

        self.adult_works = {}
        self.ya_works = {}
        self.childrens_works = {}

        for genre in (self.fantasy, self.epic_fantasy, self.urban_fantasy,
                      self.history, self.african_history):
            fiction = True
            if genre in (self.history, self.african_history):
                fiction = False

            # Create a number of books for each genre.
            adult_work = self._work(
                title="%s Adult" % genre.name, 
                audience=Lane.AUDIENCE_ADULT,
                fiction=fiction,
                with_license_pool=True,
                genre=genre,
            )
            self.adult_works[genre] = adult_work
            adult_work.simple_opds_entry = '<entry>'

            # Childrens and YA books need to be attached to a data
            # source other than Gutenberg, or they'll get filtered
            # out.
            ya_edition, lp = self._edition(
                title="%s YA" % genre.name,                 
                data_source_name=DataSource.OVERDRIVE,
                with_license_pool=True
            )
            ya_work = self._work(
                audience=Lane.AUDIENCE_YOUNG_ADULT,
                fiction=fiction,
                with_license_pool=True,
                presentation_edition=ya_edition,
                genre=genre,
            )
            self.ya_works[genre] = ya_work
            ya_work.simple_opds_entry = '<entry>'

            childrens_edition, lp = self._edition(
                title="%s Childrens" % genre.name,
                data_source_name=DataSource.OVERDRIVE, with_license_pool=True
            )
            childrens_work = self._work(
                audience=Lane.AUDIENCE_CHILDREN,
                fiction=fiction,
                with_license_pool=True,
                presentation_edition=childrens_edition,
                genre=genre,
            )
            if genre == self.epic_fantasy:
                childrens_work.target_age = NumericRange(7, 9, '[]')
            else:
                childrens_work.target_age = NumericRange(8, 10, '[]')
            self.childrens_works[genre] = childrens_work
            childrens_work.simple_opds_entry = '<entry>'

        # Create generic 'Adults Only' fiction and nonfiction books
        # that are not in any genre.
        self.nonfiction = self._work(
            title="Generic Nonfiction", fiction=False,
            audience=Lane.AUDIENCE_ADULTS_ONLY,
            with_license_pool=True
        )
        self.nonfiction.simple_opds_entry = '<entry>'
        self.fiction = self._work(
            title="Generic Fiction", fiction=True,
            audience=Lane.AUDIENCE_ADULTS_ONLY,
            with_license_pool=True
        )
        self.fiction.simple_opds_entry = '<entry>'

        # Create a work of music.
        self.music = self._work(
            title="Music", fiction=False,
            audience=Lane.AUDIENCE_ADULT,
            with_license_pool=True,
        )
        self.music.presentation_edition.medium=Edition.MUSIC_MEDIUM
        self.music.simple_opds_entry = '<entry>'

        # Create a Spanish book.
        self.spanish = self._work(
            title="Spanish book", fiction=True,
            audience=Lane.AUDIENCE_ADULT,
            with_license_pool=True,
            language='spa'
        )
        self.spanish.simple_opds_entry = '<entry>'

        # Refresh the materialized views so that all these books are present
        # in them.
        SessionManager.refresh_materialized_views(self._db)

    def test_lanes(self):
        # I'm putting all these tests into one method because the
        # setup function is so very expensive.

        def _assert_expectations(lane, expected_count, predicate,
                              mw_predicate=None):
            """Ensure that a database query and a query of the
            materialized view give the same results.
            """
            mw_predicate = mw_predicate or predicate
            w = lane.works().all()
            mw = lane.materialized_works().all()
            eq_(len(w), expected_count)
            eq_(len(mw), expected_count)
            assert all([predicate(x) for x in w])
            assert all([mw_predicate(x) for x in mw])
            return w, mw

        # The 'everything' lane contains 18 works -- everything except
        # the music.
        lane = Lane(self._db, self._default_library, "Everything")
        w, mw = _assert_expectations(lane, 18, lambda x: True)

        # The 'Spanish' lane contains 1 book.
        lane = Lane(self._db, self._default_library, "Spanish", languages='spa')
        eq_(['spa'], lane.languages)
        w, mw = _assert_expectations(lane, 1, lambda x: True)
        eq_([self.spanish], w)

        # The 'everything except English' lane contains that same book.
        lane = Lane(self._db, self._default_library, "Not English", exclude_languages='eng')
        eq_(None, lane.languages)
        eq_(['eng'], lane.exclude_languages)
        w, mw = _assert_expectations(lane, 1, lambda x: True)
        eq_([self.spanish], w)

        # The 'music' lane contains 1 work of music
        lane = Lane(self._db, self._default_library, "Music", media=Edition.MUSIC_MEDIUM)
        w, mw = _assert_expectations(
            lane, 1, 
            lambda x: x.presentation_edition.medium==Edition.MUSIC_MEDIUM,
            lambda x: x.medium==Edition.MUSIC_MEDIUM
        )
        
        # The 'English fiction' lane contains ten fiction books.
        lane = Lane(self._db, self._default_library, "English Fiction", fiction=True, languages='eng')
        w, mw = _assert_expectations(
            lane, 10, lambda x: x.fiction
        )

        # The 'nonfiction' lane contains seven nonfiction books.
        # It does not contain the music.
        lane = Lane(self._db, self._default_library, "Nonfiction", fiction=False)
        w, mw = _assert_expectations(
            lane, 7, 
            lambda x: x.presentation_edition.medium==Edition.BOOK_MEDIUM and not x.fiction,
            lambda x: x.medium==Edition.BOOK_MEDIUM and not x.fiction
        )

        # The 'adults' lane contains five books for adults.
        lane = Lane(self._db, self._default_library, "Adult English",
                    audiences=Lane.AUDIENCE_ADULT, languages='eng')
        w, mw = _assert_expectations(
            lane, 5, lambda x: x.audience==Lane.AUDIENCE_ADULT
        )

        # This lane contains those five books plus two adults-only
        # books.
        audiences = [Lane.AUDIENCE_ADULT, Lane.AUDIENCE_ADULTS_ONLY]
        lane = Lane(self._db, self._default_library, "Adult + Adult Only",
                    audiences=audiences, languages='eng'
        )
        w, mw = _assert_expectations(
            lane, 7, lambda x: x.audience in audiences
        )
        eq_(2, len([x for x in w if x.audience==Lane.AUDIENCE_ADULTS_ONLY]))
        eq_(2, len([x for x in mw if x.audience==Lane.AUDIENCE_ADULTS_ONLY]))

        # The 'Young Adults' lane contains five books.
        lane = Lane(self._db, self._default_library, "Young Adults", 
                    audiences=Lane.AUDIENCE_YOUNG_ADULT)
        w, mw = _assert_expectations(
            lane, 5, lambda x: x.audience==Lane.AUDIENCE_YOUNG_ADULT
        )

        # There is one book suitable for seven-year-olds.
        lane = Lane(
            self._db, self._default_library, "If You're Seven", audiences=Lane.AUDIENCE_CHILDREN,
            age_range=7
        )
        w, mw = _assert_expectations(
            lane, 1, lambda x: x.audience==Lane.AUDIENCE_CHILDREN
        )

        # There are four books suitable for ages 10-12.
        lane = Lane(
            self._db, self._default_library, "10-12", audiences=Lane.AUDIENCE_CHILDREN,
            age_range=(10,12)
        )
        w, mw = _assert_expectations(
            lane, 4, lambda x: x.audience==Lane.AUDIENCE_CHILDREN
        )
       
        #
        # Now let's start messing with genres.
        #

        # Here's an 'adult fantasy' lane, in which the subgenres of Fantasy
        # are kept in the same place as generic Fantasy.
        lane = Lane(
            self._db, self._default_library, "Adult Fantasy",
            genres=[self.fantasy], 
            subgenre_behavior=Lane.IN_SAME_LANE,
            fiction=Lane.FICTION_DEFAULT_FOR_GENRE,
            audiences=Lane.AUDIENCE_ADULT,
        )
        # We get three books: Fantasy, Urban Fantasy, and Epic Fantasy.
        w, mw = _assert_expectations(
            lane, 3, lambda x: True
        )
        expect = [u'Epic Fantasy Adult', u'Fantasy Adult', u'Urban Fantasy Adult']
        eq_(expect, sorted([x.sort_title for x in w]))
        eq_(expect, sorted([x.sort_title for x in mw]))

        # Here's a 'YA fantasy' lane in which urban fantasy is explicitly
        # excluded (maybe because it has its own separate lane).
        lane = Lane(
            self._db, self._default_library, full_name="Adult Fantasy",
            genres=[self.fantasy], 
            exclude_genres=[self.urban_fantasy],
            subgenre_behavior=Lane.IN_SAME_LANE,
            fiction=Lane.FICTION_DEFAULT_FOR_GENRE,
            audiences=Lane.AUDIENCE_YOUNG_ADULT,
        )

        # Urban Fantasy does not show up in this lane's genres.
        eq_(
            ["Epic Fantasy", "Fantasy", "Historical Fantasy"], 
            sorted(lane.genre_names)
        )

        # We get two books: Fantasy and Epic Fantasy.
        w, mw = _assert_expectations(
            lane, 2, lambda x: True
        )
        expect = [u'Epic Fantasy YA', u'Fantasy YA']
        eq_(expect, sorted([x.sort_title for x in w]))
        eq_(sorted([x.id for x in w]), sorted([x.works_id for x in mw]))

        # Try a lane based on license source.
        overdrive = DataSource.lookup(self._db, DataSource.OVERDRIVE)
        lane = Lane(self._db, self._default_library, full_name="Overdrive Books",
                    license_source=overdrive)
        w, mw = _assert_expectations(
            lane, 10, lambda x: True
        )
        for i in mw:
            eq_(i.data_source_id, overdrive.id)
        for i in w:
            eq_(i.license_pools[0].data_source, overdrive)


        # Finally, test lanes based on lists. Create two lists, each
        # with one book.
        one_day_ago = datetime.datetime.utcnow() - datetime.timedelta(days=1)
        one_year_ago = datetime.datetime.utcnow() - datetime.timedelta(days=365)

        fic_name = "Best Sellers - Fiction"
        best_seller_list_1, ignore = self._customlist(
            foreign_identifier=fic_name, name=fic_name,
            num_entries=0
        )
        best_seller_list_1.add_entry(
            self.fiction.presentation_edition, first_appearance=one_day_ago
        )
        
        nonfic_name = "Best Sellers - Nonfiction"
        best_seller_list_2, ignore = self._customlist(
            foreign_identifier=nonfic_name, name=nonfic_name, num_entries=0
        )
        best_seller_list_2.add_entry(
            self.nonfiction.presentation_edition, first_appearance=one_year_ago
        )

        # Create a lane for one specific list
        fiction_best_sellers = Lane(
            self._db, self._default_library, full_name="Fiction Best Sellers",
            list_identifier=fic_name
        )
        w, mw = _assert_expectations(
            fiction_best_sellers, 1, 
            lambda x: x.sort_title == self.fiction.sort_title
        )

        # Create a lane for all best-sellers.
        all_best_sellers = Lane(
            self._db, self._default_library, full_name="All Best Sellers",
            list_data_source=best_seller_list_1.data_source.name
        )
        w, mw = _assert_expectations(
            all_best_sellers, 2, 
            lambda x: x.sort_title in (
                self.fiction.sort_title, self.nonfiction.sort_title
            )
        )

        # Combine list membership with another criteria (nonfiction)
        all_nonfiction_best_sellers = Lane(
            self._db, self._default_library, full_name="All Nonfiction Best Sellers",
            fiction=False,
            list_data_source=best_seller_list_1.data_source.name
        )
        w, mw = _assert_expectations(
            all_nonfiction_best_sellers, 1, 
            lambda x: x.sort_title==self.nonfiction.sort_title
        )

        # Apply a cutoff date to a best-seller list,
        # excluding the work that was last seen a year ago.
        best_sellers_past_week = Lane(
            self._db, self._default_library, full_name="Best Sellers - The Past Week",
            list_data_source=best_seller_list_1.data_source.name,
            list_seen_in_previous_days=7
        )
        w, mw = _assert_expectations(
            best_sellers_past_week, 1, 
            lambda x: x.sort_title==self.fiction.sort_title
        )
  
    def test_from_description(self):
        """Create a LaneList from a simple description."""
        lanes = LaneList.from_description(
            self._db,
            self._default_library,
            None,
            [dict(
                full_name="Fiction",
                fiction=True,
                audiences=Classifier.AUDIENCE_ADULT,
            ),
             classifier.Fantasy,
             dict(
                 full_name="Young Adult",
                 fiction=Lane.BOTH_FICTION_AND_NONFICTION,
                 audiences=Classifier.AUDIENCE_YOUNG_ADULT,
             ),
         ]
        )

        fantasy_genre, ignore = Genre.lookup(self._db, classifier.Fantasy.name)
        urban_fantasy_genre, ignore = Genre.lookup(self._db, classifier.Urban_Fantasy.name)

        fiction = lanes.by_languages['']['Fiction']
        young_adult = lanes.by_languages['']['Young Adult']
        fantasy = lanes.by_languages['']['Fantasy'] 
        urban_fantasy = lanes.by_languages['']['Urban Fantasy'] 

        eq_(set([fantasy, fiction, young_adult]), set(lanes.lanes))

        eq_("Fiction", fiction.name)
        eq_(set([Classifier.AUDIENCE_ADULT]), fiction.audiences)
        eq_([], fiction.genre_ids)
        eq_(True, fiction.fiction)

        eq_("Fantasy", fantasy.name)
        eq_(set(), fantasy.audiences)
        expect = set(x.name for x in fantasy_genre.self_and_subgenres)
        eq_(expect, set(fantasy.genre_names))
        eq_(True, fantasy.fiction)

        eq_("Urban Fantasy", urban_fantasy.name)
        eq_(set(), urban_fantasy.audiences)
        eq_([urban_fantasy_genre.id], urban_fantasy.genre_ids)
        eq_(True, urban_fantasy.fiction)

        eq_("Young Adult", young_adult.name)
        eq_(set([Classifier.AUDIENCE_YOUNG_ADULT]), young_adult.audiences)
        eq_([], young_adult.genre_ids)
        eq_(Lane.BOTH_FICTION_AND_NONFICTION, young_adult.fiction)


class TestFilters(DatabaseTest):

    def test_only_show_ready_deliverable_works(self):
        # w1 has licenses but no available copies. It's available
        # unless site policy is to hide books like this.
        w1 = self._work(with_license_pool=True)
        w1.presentation_edition.title = 'I have no available copies'
        w1.license_pools[0].open_access = False
        w1.license_pools[0].licenses_owned = 10
        w1.license_pools[0].licenses_available = 0

        # w2 has no delivery mechanisms.
        w2 = self._work(with_license_pool=True, with_open_access_download=False)
        w2.presentation_edition.title = 'I have no delivery mechanisms'
        for dm in w2.license_pools[0].delivery_mechanisms:
            self._db.delete(dm)

        # w3 is not presentation ready.
        w3 = self._work(with_license_pool=True)
        w3.presentation_edition.title = "I'm not presentation ready"
        w3.presentation_ready = False

        # w4's only license pool is suppressed.
        w4 = self._work(with_open_access_download=True)
        w4.presentation_edition.title = "I am suppressed"
        w4.license_pools[0].suppressed = True

        # w5 has no licenses.
        w5 = self._work(with_license_pool=True)
        w5.presentation_edition.title = "I have no owned licenses."
        w5.license_pools[0].open_access = False
        w5.license_pools[0].licenses_owned = 0

        # w6 is an open-access book, so it's available even though
        # licenses_owned and licenses_available are zero.
        w6 = self._work(with_open_access_download=True)
        w6.presentation_edition.title = "I'm open-access."
        w6.license_pools[0].open_access = True
        w6.license_pools[0].licenses_owned = 0
        w6.license_pools[0].licenses_available = 0

        # w7 is not open-access. We own licenses for it, and there are
        # licenses available right now. It's available.
        w7 = self._work(with_license_pool=True)
        w7.presentation_edition.title = "I have available licenses."
        w7.license_pools[0].open_access = False
        w7.license_pools[0].licenses_owned = 9
        w7.license_pools[0].licenses_available = 5

        # w8 has a delivery mechanism that can't be rendered by the
        # default client.
        w8 = self._work(with_license_pool=True)
        w8.presentation_edition.title = "I have a weird delivery mechanism"
        [pool] = w8.license_pools
        for dm in pool.delivery_mechanisms:
            self._db.delete(dm)
        self._db.commit()
        pool.set_delivery_mechanism(
            "weird content type", "weird DRM scheme", "weird rights URI",
            None
        )

        # w9 is in a collection not associated with the default library.
        w9 = self._work(with_license_pool=True)
        collection2 = self._collection()
        w9.license_pools[0].collection = collection2
        w9.presentation_edition.title = "I'm in a different collection"
        
        # A normal query against Work/LicensePool finds all works.
        orig_q = self._db.query(Work).join(Work.license_pools)
        eq_(9, orig_q.count())

        # only_show_ready_deliverable_works filters out everything but
        # w1 (owned licenses), w6 (open-access), w7 (available
        # licenses), and w8 (available licenses, weird delivery mechanism).
        library = self._default_library
        lane = Lane(self._db, library, self._str)
        q = lane.only_show_ready_deliverable_works(orig_q, Work)
        eq_(set([w1, w6, w7, w8]), set(q.all()))

        # If we decide to show suppressed works, w4 shows up as well.
        q = lane.only_show_ready_deliverable_works(
            orig_q, Work, show_suppressed=True
        )
        eq_(set([w1, w4, w6, w7, w8]), set(q.all()))

        # Change site policy to hide books that can't be borrowed.
        library.setting(Library.ALLOW_HOLDS).value = "False"
        # w1 no longer shows up, because although we own licenses, 
        #  no copies are available.
        # w4 is open-access but it's suppressed, so it still doesn't 
        #  show up.
        # w6 still shows up because it's an open-access work.
        # w7 and w8 show up because we own licenses and copies are
        #  available.
        q = lane.only_show_ready_deliverable_works(orig_q, Work)
        eq_(set([w6, w7, w8]), set(q.all()))

        # If we add the second collection to the library, its works
        # start showing up. (But we have to recreate the Lane object
        # because it only looks at the library's collections during
        # construction.)
        library.setting(Library.ALLOW_HOLDS).value = "True"
        library.collections.append(collection2)
        lane = Lane(self._db, library, self._str)
        q = lane.only_show_ready_deliverable_works(orig_q, Work)
        eq_(set([w1, w6, w7, w8, w9]), set(q.all()))
        
    def test_lane_subclass_queries(self):
        """Subclasses of Lane can effectively retrieve all of a Work's
        LicensePools
        """
        class LaneSubclass(Lane):
            """A subclass of Lane that filters against a
            LicensePool-specific criteria
            """
            def apply_filters(self, qu, **kwargs):
                return qu.filter(DataSource.name==DataSource.GUTENBERG)

        # Create a work with two license_pools. One that fits the
        # LaneSubclass criteria and one that doesn't.
        w1 = self._work(with_open_access_download=True)
        _edition, additional_lp = self._edition(
            data_source_name=DataSource.OVERDRIVE,
            identifier_type=Identifier.OVERDRIVE_ID,
            with_license_pool=True,
            with_open_access_download=True
        )
        additional_lp.work = w1
        self._db.commit()

        # When the work is queried, both of the LicensePools are
        # available in the database session, despite the filtering.
        subclass = LaneSubclass(self._db, self._default_library, "Lane Subclass")
        [subclass_work] = subclass.works().all()
        eq_(2, len(subclass_work.license_pools))
