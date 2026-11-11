import { Navbar } from './app/navigation/Navbar';
import { OrdersFilter } from '@demo/monokit/features/orders-filter/OrdersFilter';
import { PageTitle } from '@demo/monokit/shared/ui/page-title/PageTitle';
import { localeNames } from '@demo/monokit/i18n/i18n';

export const app = [Navbar, OrdersFilter, PageTitle, localeNames];
