import unittest
from shopping_agent.qualification import numeric, normalize, check


def product(field, text, category='headphones', title='', source=None):
    return {'category_id':category, 'title':title, 'attributes':{field:{'evidence':{'field':source or 'details.'+field,'text':text}}}}


def req(value, operator='contains', unit=None):
    return {'value':{'operator':operator,'value':value,'unit':unit},'status':'active','strength':'hard'}


class QualificationTests(unittest.TestCase):
    def test_decimal_boundary_and_units(self):
        p=product('capacity','1500 Milliliters')
        self.assertEqual(check(p,'capacity',req('1.5','gte','L'))['status'],'satisfied')
        self.assertEqual(check(p,'capacity',req('1.5','gt','L'))['status'],'violated')
        self.assertEqual(numeric('weight','1 pounds')['value'],'453.59237')
        self.assertEqual(numeric('battery_life','2 hours')['value'],'1.2E+2')

    def test_ambiguous_units_fail_closed(self):
        for field,text in [('capacity','6 Cups'),('capacity','20 ounces'),('weight','2'),('power','800'),('voltage','110-240V')]:
            with self.subTest(text=text): self.assertIsNone(numeric(field,text))
        self.assertEqual(numeric('power','800','details.Wattage')['unit'],'W')

    def test_conflicting_capacity_is_not_rounded_into_pass(self):
        p=product('capacity','1.69 L','electric_kettles','Kettle 1.7L')
        self.assertEqual(check(p,'capacity',req('1.7','gte','L'))['status'],'conflict')

    def test_conflicting_ip_and_negation(self):
        self.assertEqual(normalize(product('waterproof','IPX4 or IPX7'),'waterproof')['status'],'conflict')
        self.assertEqual(normalize(product('waterproof','not IPX7'),'waterproof')['status'],'unknown')
        p={'title':'Not IPX7 waterproof','attributes':{}}
        self.assertEqual(normalize(p,'waterproof')['status'],'unknown')

    def test_open_set_absence_is_unknown(self):
        p=product('connectivity','USB')
        self.assertEqual(check(p,'connectivity',req('wireless'))['status'],'unknown')
        self.assertEqual(check(p,'connectivity',req('bluetooth','not_contains'))['status'],'unknown')
        self.assertEqual(check(product('form_factor','In Ear'),'form_factor',req('over_ear'))['status'],'violated')

    def test_marketing_and_component_claims_not_inferred(self):
        self.assertEqual(check(product('keyboard_description','mechanical-feel'),'keyboard_description',req('mechanical'))['status'],'unknown')
        self.assertEqual(check(product('material','Stainless Steel'),'material',req('纯不锈钢内胆'))['status'],'unknown')
        self.assertEqual(check(product('compatible_devices','PC, Tablet'),'compatible_devices',req('iPad 2024 tablet'))['status'],'unknown')
        self.assertEqual(check(product('connectivity','not wireless'),'connectivity',req('wireless'))['status'],'unknown')

    def test_explicit_source_properties_across_categories(self):
        for field,text,value in [('connectivity','Bluetooth','wireless'),('form_factor','Over Ear','over_ear'),('material','Nylon','nylon'),('power_source','Battery Powered','battery'),('tracking','Optical','optical'),('vacuum_type','Handheld','handheld'),('light_source','LED','led'),('age_range','Adult','adult'),('shaving_use','Beard','beard'),('head_type','Rotary','rotary'),('speaker_type','Outdoor','outdoor'),('keyboard_description','Mechanical','mechanical')]:
            with self.subTest(field=field): self.assertEqual(check(product(field,text),field,req(value))['status'],'satisfied')

    def test_actual_catalog_all_recommended_hard_checks_have_evidence(self):
        from shopping_agent.catalog import Catalog
        from pathlib import Path
        catalog=Catalog(Path(__file__).resolve().parents[1]/'data/amazon/catalog_v1')
        covered=set()
        from shopping_agent.qualification import CATEGORY_FIELDS
        for p in catalog._products.values():
            for field in CATEGORY_FIELDS[p['category_id']]:
                result=normalize(p,field)
                if result['status']=='known':
                    self.assertTrue(result['evidence'])
                    for evidence in result['evidence']:
                        raw=catalog._field(catalog._sources[p['id']]['raw'],evidence['field'])
                        self.assertIn(evidence['text'],str(raw))
                    covered.add(p['category_id'])
        self.assertEqual(covered,set(CATEGORY_FIELDS))

    def test_title_detail_form_factor_disagreement_is_conflict(self):
        p=product('form_factor','Over Ear',title='Wireless On-Ear Headphones')
        self.assertEqual(check(p,'form_factor',req('over_ear'))['status'],'conflict')
        p=product('form_factor','Over Ear',title='True Wireless Earbuds')
        self.assertEqual(check(p,'form_factor',req('over_ear'))['status'],'conflict')
