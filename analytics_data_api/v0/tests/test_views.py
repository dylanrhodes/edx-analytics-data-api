# NOTE: Full URLs are used throughout these tests to ensure that the API contract is fulfilled. The URLs should *not*
# change for versions greater than 1.0.0. Tests target a specific version of the API, additional tests should be added
# for subsequent versions if there are breaking changes introduced in those versions.
import StringIO
import csv
import datetime
from itertools import groupby
import urllib

from django.conf import settings
from django_dynamic_fixture import G
from iso3166 import countries
import pytz
from opaque_keys.edx.keys import CourseKey

from analytics_data_api.v0 import models
from analytics_data_api.v0.constants import UNKNOWN_COUNTRY, UNKNOWN_COUNTRY_CODE
from analytics_data_api.v0.models import CourseActivityWeekly
from analytics_data_api.v0.serializers import ProblemResponseAnswerDistributionSerializer
from analytics_data_api.v0.tests.utils import flatten
from analyticsdataserver.tests import TestCaseWithAuthentication


DEMO_COURSE_ID = 'course-v1:edX+DemoX+Demo_2014'


class DemoCourseMixin(object):
    course_key = None
    course_id = None

    def setUp(self):
        self.course_id = DEMO_COURSE_ID
        self.course_key = CourseKey.from_string(self.course_id)
        super(DemoCourseMixin, self).setUp()


# pylint: disable=no-member
class CourseViewTestCaseMixin(DemoCourseMixin):
    model = None
    api_root_path = '/api/v0/'
    path = None
    order_by = []
    csv_filename_slug = None

    def generate_data(self, course_id=None):
        raise NotImplementedError

    def format_as_response(self, *args):
        """
        Format given data as a response that would be issued by the endpoint.

        Arguments
            args    --  Iterable list of objects
        """
        raise NotImplementedError

    def get_latest_data(self, course_id=None):
        """
        Return the latest row/rows that would be returned if a user made a call
        to the endpoint with no date filtering.

        Return value must be an iterable.
        """
        raise NotImplementedError

    def get_csv_filename(self):
        return u'edX-DemoX-Demo_2014--{0}.csv'.format(self.csv_filename_slug)

    def test_get_not_found(self):
        """ Requests made against non-existent courses should return a 404 """
        course_id = 'edX/DemoX/Non_Existent_Course'
        response = self.authenticated_get('%scourses/%s%s' % (self.api_root_path, course_id, self.path))
        self.assertEquals(response.status_code, 404)

    def test_get(self):
        """ Verify the endpoint returns an HTTP 200 status and the correct data. """
        # Validate the basic response status
        response = self.authenticated_get('%scourses/%s%s' % (self.api_root_path, self.course_id, self.path))
        self.assertEquals(response.status_code, 200)

        # Validate the data is correct and sorted chronologically
        expected = self.format_as_response(*self.get_latest_data())
        self.assertEquals(response.data, expected)

    def assertCSVIsValid(self, course_id, filename):
        path = '{0}courses/{1}{2}'.format(self.api_root_path, course_id, self.path)
        csv_content_type = 'text/csv'
        response = self.authenticated_get(path, HTTP_ACCEPT=csv_content_type)

        # Validate the basic response status, content type, and filename
        self.assertEquals(response.status_code, 200)
        self.assertEquals(response['Content-Type'].split(';')[0], csv_content_type)
        self.assertEquals(response['Content-Disposition'], u'attachment; filename={}'.format(filename))

        # Validate the actual data
        data = self.format_as_response(*self.get_latest_data(course_id=course_id))
        data = map(flatten, data)

        # The CSV renderer sorts the headers alphabetically
        fieldnames = sorted(data[0].keys())

        # Generate the expected CSV output
        expected = StringIO.StringIO()
        writer = csv.DictWriter(expected, fieldnames)
        writer.writeheader()
        writer.writerows(data)
        self.assertEqual(response.content, expected.getvalue())

    def test_get_csv(self):
        """ Verify the endpoint returns data that has been properly converted to CSV. """
        self.assertCSVIsValid(self.course_id, self.get_csv_filename())

    def test_get_csv_with_deprecated_key(self):
        """
        Verify the endpoint returns data that has been properly converted to CSV even if the course ID is deprecated.
        """
        course_id = u'edX/DemoX/Demo_Course'
        self.generate_data(course_id)
        filename = u'{0}--{1}.csv'.format(u'edX-DemoX-Demo_Course', self.csv_filename_slug)
        self.assertCSVIsValid(course_id, filename)

    def test_get_with_intervals(self):
        """ Verify the endpoint returns multiple data points when supplied with an interval of dates. """
        raise NotImplementedError

    def assertIntervalFilteringWorks(self, expected_response, start_date, end_date):
        # If start date is after date of existing data, no data should be returned
        date = (start_date + datetime.timedelta(days=30)).strftime(settings.DATE_FORMAT)
        response = self.authenticated_get(
            '%scourses/%s%s?start_date=%s' % (self.api_root_path, self.course_id, self.path, date))
        self.assertEquals(response.status_code, 200)
        self.assertListEqual([], response.data)

        # If end date is before date of existing data, no data should be returned
        date = (start_date - datetime.timedelta(days=30)).strftime(settings.DATE_FORMAT)
        response = self.authenticated_get(
            '%scourses/%s%s?end_date=%s' % (self.api_root_path, self.course_id, self.path, date))
        self.assertEquals(response.status_code, 200)
        self.assertListEqual([], response.data)

        # If data falls in date range, data should be returned
        start_date = start_date.strftime(settings.DATE_FORMAT)
        end_date = end_date.strftime(settings.DATE_FORMAT)
        response = self.authenticated_get('%scourses/%s%s?start_date=%s&end_date=%s' % (
            self.api_root_path, self.course_id, self.path, start_date, end_date))
        self.assertEquals(response.status_code, 200)
        self.assertListEqual(response.data, expected_response)


# pylint: disable=abstract-method
class CourseEnrollmentViewTestCaseMixin(CourseViewTestCaseMixin):
    date = None

    def setUp(self):
        super(CourseEnrollmentViewTestCaseMixin, self).setUp()
        self.date = datetime.date(2014, 1, 1)

    def get_latest_data(self, course_id=None):
        course_id = course_id or self.course_id
        return self.model.objects.filter(course_id=course_id, date=self.date).order_by('date', *self.order_by)

    def test_get_with_intervals(self):
        expected = self.format_as_response(*self.model.objects.filter(date=self.date))
        self.assertIntervalFilteringWorks(expected, self.date, self.date + datetime.timedelta(days=1))


class CourseActivityLastWeekTest(DemoCourseMixin, TestCaseWithAuthentication):
    def generate_data(self, course_id=None):
        course_id = course_id or self.course_id
        interval_start = datetime.datetime(2014, 1, 1, tzinfo=pytz.utc)
        interval_end = interval_start + datetime.timedelta(weeks=1)
        # G(models.CourseActivityWeekly, course_id=course_id, interval_start=interval_start,
        # interval_end=interval_end,
        # activity_type='POSTED_FORUM', count=100)
        G(models.CourseActivityWeekly, course_id=course_id, interval_start=interval_start,
          interval_end=interval_end,
          activity_type='ATTEMPTED_PROBLEM', count=200)
        G(models.CourseActivityWeekly, course_id=course_id, interval_start=interval_start,
          interval_end=interval_end,
          activity_type='ACTIVE', count=300)
        G(models.CourseActivityWeekly, course_id=course_id, interval_start=interval_start,
          interval_end=interval_end,
          activity_type='PLAYED_VIDEO', count=400)

    def setUp(self):
        super(CourseActivityLastWeekTest, self).setUp()
        self.generate_data()

    def test_activity(self):
        response = self.authenticated_get('/api/v0/courses/{0}/recent_activity'.format(self.course_id))
        self.assertEquals(response.status_code, 200)
        self.assertEquals(response.data, self.get_activity_record())

    def assertValidActivityResponse(self, activity_type, count):
        response = self.authenticated_get('/api/v0/courses/{0}/recent_activity?activity_type={1}'.format(
            self.course_id, activity_type))
        self.assertEquals(response.status_code, 200)
        self.assertEquals(response.data, self.get_activity_record(activity_type=activity_type, count=count))

    @staticmethod
    def get_activity_record(**kwargs):
        default = {
            'course_id': DEMO_COURSE_ID,
            'interval_start': datetime.datetime(2014, 1, 1, 0, 0, tzinfo=pytz.utc),
            'interval_end': datetime.datetime(2014, 1, 8, 0, 0, tzinfo=pytz.utc),
            'activity_type': 'any',
            'count': 300,
        }
        default.update(kwargs)
        default['activity_type'] = default['activity_type'].lower()
        return default

    def test_activity_auth(self):
        response = self.client.get('/api/v0/courses/{0}/recent_activity'.format(self.course_id), follow=True)
        self.assertEquals(response.status_code, 401)

    def test_url_encoded_course_id(self):
        url_encoded_course_id = urllib.quote_plus(self.course_id)
        response = self.authenticated_get('/api/v0/courses/{}/recent_activity'.format(url_encoded_course_id))
        self.assertEquals(response.status_code, 200)
        self.assertEquals(response.data, self.get_activity_record())

    def test_any_activity(self):
        self.assertValidActivityResponse('ANY', 300)
        self.assertValidActivityResponse('any', 300)

    def test_video_activity(self):
        self.assertValidActivityResponse('played_video', 400)

    def test_unknown_activity(self):
        activity_type = 'missing_activity_type'
        response = self.authenticated_get('/api/v0/courses/{0}/recent_activity?activity_type={1}'.format(
            self.course_id, activity_type))
        self.assertEquals(response.status_code, 404)

    def test_unknown_course_id(self):
        response = self.authenticated_get('/api/v0/courses/{0}/recent_activity'.format('foo'))
        self.assertEquals(response.status_code, 404)

    def test_missing_course_id(self):
        response = self.authenticated_get('/api/v0/courses/recent_activity')
        self.assertEquals(response.status_code, 404)

    def test_label_parameter(self):
        activity_type = 'played_video'
        response = self.authenticated_get('/api/v0/courses/{0}/recent_activity?label={1}'.format(
            self.course_id, activity_type))
        self.assertEquals(response.status_code, 200)
        self.assertEquals(response.data, self.get_activity_record(activity_type=activity_type, count=400))


class CourseEnrollmentByBirthYearViewTests(CourseEnrollmentViewTestCaseMixin, TestCaseWithAuthentication):
    path = '/enrollment/birth_year'
    model = models.CourseEnrollmentByBirthYear
    order_by = ['birth_year']
    csv_filename_slug = u'enrollment-age'

    def generate_data(self, course_id=None):
        course_id = course_id or self.course_id
        G(self.model, course_id=course_id, date=self.date, birth_year=1956)
        G(self.model, course_id=course_id, date=self.date, birth_year=1986)
        G(self.model, course_id=course_id, date=self.date - datetime.timedelta(days=10), birth_year=1956)
        G(self.model, course_id=course_id, date=self.date - datetime.timedelta(days=10), birth_year=1986)

    def setUp(self):
        super(CourseEnrollmentByBirthYearViewTests, self).setUp()
        self.generate_data()

    def format_as_response(self, *args):
        return [
            {'course_id': str(ce.course_id), 'count': ce.count, 'date': ce.date.strftime(settings.DATE_FORMAT),
             'birth_year': ce.birth_year, 'created': ce.created.strftime(settings.DATETIME_FORMAT)} for ce in args]

    def test_get(self):
        response = self.authenticated_get('/api/v0/courses/%s%s' % (self.course_id, self.path,))
        self.assertEquals(response.status_code, 200)

        expected = self.format_as_response(*self.model.objects.filter(date=self.date))
        self.assertEquals(response.data, expected)


class CourseEnrollmentByEducationViewTests(CourseEnrollmentViewTestCaseMixin, TestCaseWithAuthentication):
    path = '/enrollment/education/'
    model = models.CourseEnrollmentByEducation
    order_by = ['education_level']
    csv_filename_slug = u'enrollment-education'

    def generate_data(self, course_id=None):
        course_id = course_id or self.course_id
        G(self.model, course_id=course_id, date=self.date, education_level=self.el1)
        G(self.model, course_id=course_id, date=self.date, education_level=self.el2)
        G(self.model, course_id=course_id, date=self.date - datetime.timedelta(days=2), education_level=self.el2)

    def setUp(self):
        super(CourseEnrollmentByEducationViewTests, self).setUp()
        self.el1 = G(models.EducationLevel, name='Doctorate', short_name='doctorate')
        self.el2 = G(models.EducationLevel, name='Top Secret', short_name='top_secret')
        self.generate_data()

    def format_as_response(self, *args):
        return [
            {'course_id': str(ce.course_id), 'count': ce.count, 'date': ce.date.strftime(settings.DATE_FORMAT),
             'education_level': {'name': ce.education_level.name, 'short_name': ce.education_level.short_name},
             'created': ce.created.strftime(settings.DATETIME_FORMAT)} for
            ce in args]


class CourseEnrollmentByGenderViewTests(CourseEnrollmentViewTestCaseMixin, TestCaseWithAuthentication):
    path = '/enrollment/gender/'
    model = models.CourseEnrollmentByGender
    order_by = ['gender']
    csv_filename_slug = u'enrollment-gender'

    def generate_data(self, course_id=None):
        course_id = course_id or self.course_id
        G(self.model, course_id=course_id, gender='m', date=self.date, count=34)
        G(self.model, course_id=course_id, gender='f', date=self.date, count=45)
        G(self.model, course_id=course_id, gender='f', date=self.date - datetime.timedelta(days=2), count=45)

    def setUp(self):
        super(CourseEnrollmentByGenderViewTests, self).setUp()
        self.generate_data()

    def format_as_response(self, *args):
        return [
            {'course_id': str(ce.course_id), 'count': ce.count, 'date': ce.date.strftime(settings.DATE_FORMAT),
             'gender': ce.gender, 'created': ce.created.strftime(settings.DATETIME_FORMAT)} for ce in args]


# pylint: disable=no-member,no-value-for-parameter
class AnswerDistributionTests(TestCaseWithAuthentication):
    path = '/answer_distribution'
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.course_id = "org/num/run"
        cls.module_id = "i4x://org/num/run/problem/RANDOMNUMBER"
        cls.part_id1 = "i4x-org-num-run-problem-RANDOMNUMBER_2_1"
        cls.ad1 = G(
            models.ProblemResponseAnswerDistribution,
            course_id=cls.course_id,
            module_id=cls.module_id,
            part_id=cls.part_id1
        )

    def test_get(self):
        response = self.authenticated_get('/api/v0/problems/%s%s' % (self.module_id, self.path))
        self.assertEquals(response.status_code, 200)

        expected_dict = ProblemResponseAnswerDistributionSerializer(self.ad1).data
        actual_list = response.data
        self.assertEquals(len(actual_list), 1)
        self.assertDictEqual(actual_list[0], expected_dict)

    def test_get_404(self):
        response = self.authenticated_get('/api/v0/problems/%s%s' % ("DOES-NOT-EXIST", self.path))
        self.assertEquals(response.status_code, 404)


class CourseEnrollmentViewTests(CourseEnrollmentViewTestCaseMixin, TestCaseWithAuthentication):
    model = models.CourseEnrollmentDaily
    path = '/enrollment'
    csv_filename_slug = u'enrollment'

    def generate_data(self, course_id=None):
        course_id = course_id or self.course_id
        G(self.model, course_id=course_id, date=self.date, count=203)
        G(self.model, course_id=course_id, date=self.date - datetime.timedelta(days=5), count=203)

    def setUp(self):
        super(CourseEnrollmentViewTests, self).setUp()
        self.generate_data()

    def format_as_response(self, *args):
        return [
            {'course_id': str(ce.course_id), 'count': ce.count, 'date': ce.date.strftime(settings.DATE_FORMAT),
             'created': ce.created.strftime(settings.DATETIME_FORMAT)}
            for ce in args]


class CourseEnrollmentByLocationViewTests(CourseEnrollmentViewTestCaseMixin, TestCaseWithAuthentication):
    path = '/enrollment/location/'
    model = models.CourseEnrollmentByCountry
    csv_filename_slug = u'enrollment-location'

    def format_as_response(self, *args):
        unknown = {'course_id': None, 'count': 0, 'date': None,
                   'country': {'alpha2': None, 'alpha3': None, 'name': UNKNOWN_COUNTRY_CODE}}

        for arg in args:
            if arg.country.name == UNKNOWN_COUNTRY_CODE:
                unknown['course_id'] = arg.course_id
                unknown['date'] = arg.date.strftime(settings.DATE_FORMAT)
                unknown['count'] += arg.count
                unknown['created'] = arg.created.strftime(settings.DATETIME_FORMAT)

        args = [arg for arg in args if arg.country != UNKNOWN_COUNTRY]
        args = sorted(args, key=lambda item: (item.date, item.course_id, item.country.alpha3))

        response = [unknown]
        response += [
            {'course_id': str(ce.course_id), 'count': ce.count, 'date': ce.date.strftime(settings.DATE_FORMAT),
             'country': {'alpha2': ce.country.alpha2, 'alpha3': ce.country.alpha3, 'name': ce.country.name},
             'created': ce.created.strftime(settings.DATETIME_FORMAT)} for ce in
            args]

        return response

    def generate_data(self, course_id=None):
        course_id = course_id or self.course_id
        G(self.model, course_id=course_id, country_code='US', count=455, date=self.date)
        G(self.model, course_id=course_id, country_code='CA', count=356, date=self.date)
        G(self.model, course_id=course_id, country_code='IN', count=12, date=self.date - datetime.timedelta(days=29))
        G(self.model, course_id=course_id, country_code='', count=356, date=self.date)
        G(self.model, course_id=course_id, country_code='A1', count=1, date=self.date)
        G(self.model, course_id=course_id, country_code='A2', count=2, date=self.date)
        G(self.model, course_id=course_id, country_code='AP', count=1, date=self.date)
        G(self.model, course_id=course_id, country_code='EU', count=4, date=self.date)
        G(self.model, course_id=course_id, country_code='O1', count=7, date=self.date)

    def setUp(self):
        super(CourseEnrollmentByLocationViewTests, self).setUp()
        self.country = countries.get('US')
        self.generate_data()


class CourseActivityWeeklyViewTests(CourseViewTestCaseMixin, TestCaseWithAuthentication):
    path = '/activity/'
    default_order_by = 'interval_end'
    model = CourseActivityWeekly
    # activity_types = ['ACTIVE', 'ATTEMPTED_PROBLEM', 'PLAYED_VIDEO', 'POSTED_FORUM']
    activity_types = ['ACTIVE', 'ATTEMPTED_PROBLEM', 'PLAYED_VIDEO']
    csv_filename_slug = u'engagement-activity'

    def generate_data(self, course_id=None):
        course_id = course_id or self.course_id

        for activity_type in self.activity_types:
            G(CourseActivityWeekly,
              course_id=course_id,
              interval_start=self.interval_start,
              interval_end=self.interval_end,
              activity_type=activity_type,
              count=100)

    def setUp(self):
        super(CourseActivityWeeklyViewTests, self).setUp()
        self.interval_start = datetime.datetime(2014, 1, 1, tzinfo=pytz.utc)
        self.interval_end = self.interval_start + datetime.timedelta(weeks=1)

        self.generate_data()

    def get_latest_data(self, course_id=None):
        course_id = course_id or self.course_id
        return self.model.objects.filter(course_id=course_id, interval_end=self.interval_end)

    def format_as_response(self, *args):
        response = []

        # Group by date
        for _key, group in groupby(args, lambda x: x.interval_end):
            # Iterate over groups and create a single item with all activity types
            item = {}

            for activity in group:
                activity_type = activity.activity_type.lower()
                if activity_type == 'active':
                    activity_type = 'any'

                item.update({
                    u'course_id': activity.course_id,
                    u'interval_start': activity.interval_start.strftime(settings.DATETIME_FORMAT),
                    u'interval_end': activity.interval_end.strftime(settings.DATETIME_FORMAT),
                    u'created': activity.created.strftime(settings.DATETIME_FORMAT),
                    activity_type: activity.count
                })

            response.append(item)

        return response

    def test_get_with_intervals(self):
        """ Verify the endpoint returns multiple data points when supplied with an interval of dates. """
        # Create additional data
        interval_start = self.interval_start + datetime.timedelta(weeks=1)
        interval_end = self.interval_end + datetime.timedelta(weeks=1)

        for activity_type in self.activity_types:
            G(CourseActivityWeekly,
              course_id=self.course_id,
              interval_start=interval_start,
              interval_end=interval_end,
              activity_type=activity_type,
              count=200)

        expected = self.format_as_response(*self.model.objects.all())
        self.assertEqual(len(expected), 2)
        self.assertIntervalFilteringWorks(expected, self.interval_start, interval_end + datetime.timedelta(days=1))
